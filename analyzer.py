# analyzer.py
"""
5단계 순차 비교 파이프라인 + Union-Find 그룹화 엔진

핵심 아이디어: "값싼 비교로 먼저 걸러내고, 살아남은 후보에만 비싼 비교를 한다."

  0단계  파일 목록 수집 후 크기 오름차순 정렬 → 크기가 비슷한 파일끼리만 비교(슬라이딩 윈도우)
  1차    크기(허용 오차 % 이내) + 확장자            … stat 정보만 사용, 디스크 읽기 없음
  2차    생성일/수정일 근접도                         … stat 정보만 사용
  3차    파일명 유사도 (difflib.SequenceMatcher)      … 문자열 연산
  4차    앞부분 1MB 부분 MD5 / 이미지는 pHash         … 파일당 최대 1MB 읽기 또는 이미지 디코딩
  5차    전체 SHA-256 + 바이트(Hex) 1:1 대조           … 파일 전체 읽기 (크기가 완전히 같은 후보만)

해시 값은 FileRecord 에 캐시(지연 계산)하므로 한 파일이 여러 쌍에 등장해도 한 번만 읽습니다.
쌍(pair) 단위 판정 결과는 Union-Find 로 묶어 A≈B, B≈C 이면 A·B·C 를 한 그룹으로 만듭니다.
"""
import hashlib
import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tiff", ".tif"}
PARTIAL_HASH_BYTES = 1024 * 1024  # 4차 비교: 앞부분 1MB만 판독
FULL_CHUNK_BYTES = 64 * 1024      # 5차 비교: 64KB 버퍼 스트리밍 판독

# 진행률 콜백: (퍼센트 0~100, 상태 메시지, 현재 처리 중인 파일명)
ProgressCallback = Callable[[int, str, str], None]


@dataclass
class AnalysisConfig:
    """사용자 튜닝 가능한 5단계 분석 파라미터"""
    include_subfolders: bool = True
    min_file_size_bytes: int = 1024          # 1KB 미만 시스템/빈 파일 기본 제외
    size_tolerance_percent: float = 5.0      # 1차 비교: 크기 허용 오차 (%)
    max_time_diff_hours: float = 0.0         # 2차 비교: 0이면 시간 제한 없음, >0이면 수정/생성 시간차(시간) 필터
    filename_similarity_min: float = 0.65    # 3차 비교: 파일명 유사도 임계치 (0.0 ~ 1.0)
    enable_image_phash: bool = True          # 4차 비교: 이미지 시각적 유사도(pHash) 활성화
    phash_max_distance: int = 6              # 4차 비교: pHash 해밍 거리 허용치 (64비트 중 6비트 이하)
    strict_hex_verification: bool = True     # 5차 비교: SHA-256 일치 후 실제 바이트 데이터 교차 검증
    garbage_folder_name: str = "_Garbage_"   # 격리 폴더명 (스캔 대상에서 자동 제외)


@dataclass
class FileRecord:
    """스캔된 개별 파일의 메타데이터 및 캐시된 해시 정보"""
    file_id: int
    path: str
    name: str
    stem: str
    ext: str
    size: int
    ctime: float
    mtime: float
    depth: int
    is_image: bool
    # 지연 계산(Lazy Evaluation) 캐시: 해당 단계에 도달한 파일만 연산
    partial_md5: Optional[str] = None
    phash: Optional[int] = None
    phash_failed: bool = False
    full_sha256: Optional[str] = None


@dataclass
class DuplicateItem:
    """그룹 내 개별 파일 항목과 유사 판단 근거"""
    record: FileRecord
    match_reason: str
    confidence_score: float  # 0.0 ~ 100.0


@dataclass
class DuplicateGroup:
    """유사/중복으로 묶인 파일 그룹"""
    group_id: int
    items: List[DuplicateItem] = field(default_factory=list)

    @property
    def reclaimable_size(self) -> int:
        """가장 큰 파일 1개만 남긴다고 가정했을 때 확보되는 용량"""
        if len(self.items) <= 1:
            return 0
        sizes = sorted(item.record.size for item in self.items)
        return sum(sizes[:-1])


class UnionFind:
    """
    서로소 집합(Disjoint Set).
    쌍 단위로 "이 둘은 같은 묶음"이라고 union 해 두면, find 로 각 파일의 대표(루트)를 얻어
    전이적으로 연결된 파일들을 하나의 그룹으로 모을 수 있습니다.
    경로 압축(path halving) + 크기 기준 합치기로 연산이 사실상 상수 시간입니다.
    """

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # 조부모로 건너뛰며 경로를 절반으로 압축
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.size[rx] < self.size[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx          # 작은 트리를 큰 트리 밑에 붙여 높이를 낮게 유지
        self.size[rx] += self.size[ry]


class DuplicateAnalyzer:
    """5단계 순차 비교 파이프라인 실행기. 인스턴스 1개 = 스캔 1회."""

    def __init__(self, config: AnalysisConfig):
        self.config = config
        self.cancelled = False   # 외부(다른 스레드)에서 cancel() 로 True 가 되면 루프가 빠르게 종료
        self.total_files = 0     # 수집된(비교 대상) 파일 수
        # 2D-DCT 변환 행렬은 스캔 내내 동일하므로 한 번만 만들어 재사용 (32x32 pHash 연산 가속)
        self._dct_matrix = self._build_dct_matrix(32)

    def cancel(self) -> None:
        self.cancelled = True

    @staticmethod
    def _build_dct_matrix(n: int) -> np.ndarray:
        """직교 DCT-II 변환 행렬 C. 2D-DCT 는 C @ 픽셀 @ C.T 로 계산됩니다."""
        k = np.arange(n).reshape(-1, 1)
        i = np.arange(n).reshape(1, -1)
        matrix = np.sqrt(2.0 / n) * np.cos(np.pi * (2 * i + 1) * k / (2.0 * n))
        matrix[0, :] = np.sqrt(1.0 / n)
        return matrix

    # -------------------------------------------------------------------------
    # 지연 계산 헬퍼 (4차 · 5차 비교에 도달한 파일만 실제 디스크 I/O 발생)
    # -------------------------------------------------------------------------
    def _get_partial_md5(self, rec: FileRecord) -> Optional[str]:
        """4차 비교용: 파일 앞부분 1MB만 읽어 빠른 MD5 해시 계산 (보안 목적이 아닌 '빠른 불일치 판별'용)"""
        if rec.partial_md5 is not None:
            return rec.partial_md5
        try:
            with open(rec.path, "rb") as f:
                rec.partial_md5 = hashlib.md5(f.read(PARTIAL_HASH_BYTES)).hexdigest()
            return rec.partial_md5
        except OSError:
            return None

    def _get_image_phash(self, rec: FileRecord) -> Optional[int]:
        """
        4차 비교 특화(이미지): 32x32 2D-DCT 기반 64비트 Perceptual Hash.
          1) 흑백 변환 후 32x32 로 축소 → 해상도/포맷 차이를 제거
          2) 2D-DCT 로 주파수 영역 변환 → 좌상단 8x8 = 이미지의 큰 윤곽(저주파)
          3) 저주파 64개 값을 중앙값 기준으로 0/1 이진화 → 64비트 정수
        리사이즈·재압축·포맷 변환된 같은 그림은 이 64비트가 거의 같게 나옵니다.
        """
        if not rec.is_image or rec.phash_failed:
            return None
        if rec.phash is not None:
            return rec.phash
        try:
            with Image.open(rec.path) as img:
                gray = img.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
                pixels = np.asarray(gray, dtype=np.float64)
            dct_2d = self._dct_matrix @ pixels @ self._dct_matrix.T
            low_freq = dct_2d[:8, :8].flatten()
            # DC 성분([0,0] = 전체 평균 밝기)은 값이 지나치게 커서 중앙값 계산에서 제외
            median_val = np.median(low_freq[1:])
            phash_int = 0
            for bit in low_freq > median_val:
                phash_int = (phash_int << 1) | int(bit)
            rec.phash = phash_int
            return rec.phash
        except Exception:
            rec.phash_failed = True  # 깨진 이미지 등: 다음 쌍에서 다시 디코딩을 시도하지 않도록 표시
            return None

    def _get_full_sha256(self, rec: FileRecord) -> Optional[str]:
        """5차 비교용: 파일 전체를 64KB 씩 스트리밍하며 SHA-256 계산 (메모리 사용량 일정)"""
        if rec.full_sha256 is not None:
            return rec.full_sha256
        try:
            hasher = hashlib.sha256()
            with open(rec.path, "rb") as f:
                while True:
                    if self.cancelled:
                        return None  # 대용량 파일을 읽는 도중에도 중지 요청에 바로 반응
                    block = f.read(FULL_CHUNK_BYTES)
                    if not block:
                        break
                    hasher.update(block)
            rec.full_sha256 = hasher.hexdigest()
            return rec.full_sha256
        except OSError:
            return None

    def _verify_hex_exact_match(self, path_a: str, path_b: str) -> bool:
        """5차 비교 정밀 검증: 두 파일의 바이트 스트림을 청크 단위로 1:1 대조 (해시 충돌까지 배제)"""
        try:
            with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
                while True:
                    if self.cancelled:
                        return False
                    ba = fa.read(FULL_CHUNK_BYTES)
                    bb = fb.read(FULL_CHUNK_BYTES)
                    if ba != bb:
                        return False
                    if not ba:
                        return True
        except OSError:
            return False

    @staticmethod
    def _hamming_distance(hash1: int, hash2: int) -> int:
        """두 64비트 해시에서 서로 다른 비트 수"""
        return bin(hash1 ^ hash2).count("1")

    @staticmethod
    def _filename_similarity(stem_a: str, stem_b: str) -> float:
        a, b = stem_a.lower().strip(), stem_b.lower().strip()
        if a == b:
            return 1.0
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a, b).ratio()

    # -------------------------------------------------------------------------
    # 0단계: 파일 수집
    # -------------------------------------------------------------------------
    def _collect_files(self, root_path: Path, progress: ProgressCallback) -> List[FileRecord]:
        records: List[FileRecord] = []
        base_depth = len(root_path.parts)
        garbage_name = self.config.garbage_folder_name

        for current_root, dirs, files in os.walk(root_path):
            if self.cancelled:
                return records

            # _Garbage_ 격리 폴더와 숨김 폴더는 아예 내려가지 않음 (dirs 를 제자리 수정하면 os.walk 가 건너뜀)
            dirs[:] = [d for d in dirs if d != garbage_name and not d.startswith(".")]

            curr_path = Path(current_root)
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = curr_path / fname
                try:
                    stat = fpath.stat()
                except OSError:
                    continue  # 권한 없음/사라진 파일은 조용히 제외
                if stat.st_size < self.config.min_file_size_bytes:
                    continue
                ext = fpath.suffix.lower()
                records.append(
                    FileRecord(
                        file_id=len(records),
                        path=str(fpath),
                        name=fname,
                        stem=fpath.stem,
                        ext=ext,
                        size=stat.st_size,
                        ctime=stat.st_ctime,
                        mtime=stat.st_mtime,
                        depth=len(fpath.parts) - base_depth,
                        is_image=(ext in IMAGE_EXTENSIONS),
                    )
                )
            if len(records) % 500 < len(files):
                progress(5, f"파일 목록 수집 중... ({len(records):,}개)", curr_path.name)

            if not self.config.include_subfolders:
                break  # 최상위 폴더만 보고 종료
        return records

    # -------------------------------------------------------------------------
    # 메인: 수집 → 5단계 파이프라인 → Union-Find 그룹화
    # -------------------------------------------------------------------------
    def scan_and_analyze(
        self,
        root_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> List[DuplicateGroup]:
        progress: ProgressCallback = progress_callback or (lambda pct, msg, cur: None)
        self.cancelled = False
        root_path = Path(root_dir).resolve()

        progress(2, "파일 시스템 디렉터리 탐색 중...", "")
        records = self._collect_files(root_path, progress)
        if self.cancelled:
            return []

        total_files = self.total_files = len(records)
        if total_files < 2:
            progress(100, "비교할 파일이 충분하지 않습니다.", "")
            return []

        # 핵심 최적화: 크기 오름차순 정렬.
        # 정렬해 두면 "크기 허용 오차 안에 드는 파일"은 항상 배열에서 연속 구간이 되므로,
        # 모든 쌍(N²)을 보지 않고 i 뒤쪽의 좁은 구간(슬라이딩 윈도우)만 보면 됩니다.
        records.sort(key=lambda r: r.size)
        for idx, rec in enumerate(records):
            rec.file_id = idx

        uf = UnionFind(total_files)
        # 파일별로 "가장 신뢰도 높았던 판정 사유"만 기억 (그룹 표시용)
        best_reason: Dict[int, Tuple[float, str]] = {}
        best_rank: Dict[int, float] = {}

        cfg = self.config
        tolerance_ratio = max(0.0, min(cfg.size_tolerance_percent, 99.0)) / 100.0
        max_time_sec = cfg.max_time_diff_hours * 3600.0

        progress(10, f"총 {total_files:,}개 파일 수집 완료. 5단계 순차 비교 시작...", "")
        report_every = max(1, total_files // 100)

        for i in range(total_files):
            if self.cancelled:
                return []

            rec_a = records[i]
            if i % report_every == 0:
                pct = 10 + int((i / total_files) * 85)
                progress(pct, f"정밀 분석 중 ({i + 1:,} / {total_files:,})", rec_a.name)

            # 윈도우 상한: rec_b.size 가 이 값을 넘으면 오차 범위를 벗어난 것.
            # (오차율 = (b - a) / b <= t  ⇔  b <= a / (1 - t))
            max_allowed_size = rec_a.size if tolerance_ratio == 0 else int(rec_a.size / (1.0 - tolerance_ratio)) + 1

            for j in range(i + 1, total_files):
                rec_b = records[j]
                if rec_b.size > max_allowed_size:
                    break  # 정렬되어 있으므로 이후 파일은 전부 더 큼 → 내부 루프 즉시 탈출

                # -------------------------------------------------------------
                # [1차 비교 - 초고속/저부하] 파일 크기 및 확장자
                # -------------------------------------------------------------
                exact_size = rec_a.size == rec_b.size
                size_diff_ratio = (rec_b.size - rec_a.size) / max(rec_b.size, 1)
                if size_diff_ratio > tolerance_ratio:
                    continue

                both_images = rec_a.is_image and rec_b.is_image
                same_ext = rec_a.ext == rec_b.ext
                # 확장자가 다르면 탈락. 단, 이미지끼리는 PNG↔JPG 같은 포맷 변환본을 찾기 위해 통과
                if not same_ext and not (cfg.enable_image_phash and both_images):
                    continue

                # -------------------------------------------------------------
                # [2차 비교 - 메타데이터] 생성일 / 최종 수정일 근접도
                # -------------------------------------------------------------
                min_time_diff = min(abs(rec_a.mtime - rec_b.mtime), abs(rec_a.ctime - rec_b.ctime))
                # 시간 제한 옵션이 켜져 있으면, 크기가 다른 '유사 버전' 후보는 시간이 가까울 때만 통과.
                # 크기가 완전히 같은 후보는 복사 시점이 달라도 진짜 중복일 수 있으므로 시간으로 거르지 않음.
                if max_time_sec > 0 and not exact_size and min_time_diff > max_time_sec:
                    continue

                if min_time_diff <= 60:
                    time_note = "동일 시간대 생성/수정"
                elif min_time_diff <= 86400:
                    time_note = "24시간 이내 수정"
                else:
                    time_note = ""

                # -------------------------------------------------------------
                # [3차 비교 - 텍스트 유사도] 파일명 SequenceMatcher
                # -------------------------------------------------------------
                name_sim = self._filename_similarity(rec_a.stem, rec_b.stem)
                # 크기가 다른 일반 파일은 이름까지 다르면 무관한 파일로 보고 탈락.
                # 크기가 100% 같으면 이름이 달라도(예: '사본.pdf') 4차 해시 비교로 넘어감.
                if not exact_size and not both_images and name_sim < cfg.filename_similarity_min:
                    continue

                # -------------------------------------------------------------
                # [4차 비교 - 부분 해시 / 이미지 pHash]
                # -------------------------------------------------------------
                matched = False
                reason = ""
                confidence = 0.0

                # Case A: 이미지 → pHash 해밍 거리로 시각적 동일성 판정
                if cfg.enable_image_phash and both_images:
                    ph_a = self._get_image_phash(rec_a)
                    ph_b = self._get_image_phash(rec_b)
                    if ph_a is not None and ph_b is not None:
                        dist = self._hamming_distance(ph_a, ph_b)
                        if dist <= cfg.phash_max_distance and not (exact_size and dist == 0):
                            # (크기도 같고 pHash 도 완전히 같으면 '완전 중복' 후보이므로 아래 Case B 로 넘김)
                            visual_sim = (1.0 - dist / 64.0) * 100.0
                            fmt_info = "동일 포맷" if same_ext else f"포맷 변환({rec_a.ext}↔{rec_b.ext})"
                            matched = True
                            confidence = visual_sim
                            reason = f"시각적 동일 이미지 (pHash {visual_sim:.1f}% 일치, {fmt_info})"

                # Case B: 크기가 완전히 같은 후보 → 앞 1MB MD5 가 같을 때만 5차로 진행
                if not matched and exact_size:
                    md5_a = self._get_partial_md5(rec_a)
                    md5_b = self._get_partial_md5(rec_b)
                    if md5_a is not None and md5_a == md5_b:
                        # -----------------------------------------------------
                        # [5차 비교 - 정밀/고부하] 전체 SHA-256 + 바이트 1:1 대조
                        # -----------------------------------------------------
                        sha_a = self._get_full_sha256(rec_a)
                        sha_b = self._get_full_sha256(rec_b)
                        if sha_a is not None and sha_a == sha_b:
                            hex_ok = True
                            if cfg.strict_hex_verification:
                                hex_ok = self._verify_hex_exact_match(rec_a.path, rec_b.path)
                            if hex_ok:
                                verified = "SHA-256 & Hex" if cfg.strict_hex_verification else "SHA-256"
                                matched = True
                                confidence = 100.0
                                if name_sim >= 0.99:
                                    reason = f"완전 중복 파일 ({verified} 100% 일치)"
                                else:
                                    reason = f"내용 100% 동일 · 파일명 다름 ({verified} 일치)"

                # Case C: 크기는 조금 다르지만 이름이 매우 비슷한 파일 → '유사 버전' 의심
                if not matched and not exact_size and name_sim >= max(0.80, cfg.filename_similarity_min):
                    size_sim_pct = (1.0 - size_diff_ratio) * 100.0
                    name_sim_pct = name_sim * 100.0
                    matched = True
                    confidence = size_sim_pct * 0.5 + name_sim_pct * 0.5
                    extra = f", {time_note}" if time_note else ""
                    reason = f"유사 버전 의심 (크기 {size_sim_pct:.1f}% · 이름 {name_sim_pct:.1f}% 일치{extra})"

                if matched:
                    uf.union(i, j)  # 두 파일을 같은 집합으로 병합
                    # 신뢰도가 같으면 파일명까지 같은 쪽("완전 중복")의 사유를 우선
                    rank = confidence + name_sim * 0.001
                    for idx in (i, j):
                        if idx not in best_rank or rank > best_rank[idx]:
                            best_rank[idx] = rank
                            best_reason[idx] = (confidence, reason)

        if self.cancelled:
            return []

        # ---------------------------------------------------------------------
        # [그룹핑] Union-Find 루트가 같은 파일끼리 모아 그룹 생성
        # ---------------------------------------------------------------------
        clusters: Dict[int, List[int]] = {}
        for idx in best_reason:  # 한 번이라도 매칭된 파일만 순회
            clusters.setdefault(uf.find(idx), []).append(idx)

        groups: List[DuplicateGroup] = []
        for member_indices in clusters.values():
            if len(member_indices) < 2:
                continue
            items = [
                DuplicateItem(
                    record=records[idx],
                    match_reason=best_reason[idx][1],
                    confidence_score=best_reason[idx][0],
                )
                for idx in member_indices
            ]
            items.sort(key=lambda x: x.record.mtime, reverse=True)  # 그룹 내: 수정일 최신순
            groups.append(DuplicateGroup(group_id=0, items=items))

        # 절약 가능 용량이 큰 그룹이 위로 오도록 정렬 후 번호 부여
        groups.sort(key=lambda g: g.reclaimable_size, reverse=True)
        for number, grp in enumerate(groups, start=1):
            grp.group_id = number

        progress(100, f"분석 완료! 총 {len(groups):,}개의 중복/유사 그룹을 발견했습니다.", "")
        return groups
