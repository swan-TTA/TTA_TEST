# organizer.py
"""
안전 격리(_Garbage_) 이동 · 원위치 복구 · HTML/CSV 보고서 생성

원칙: 파일을 절대 삭제하지 않습니다. 정리 = 대상 폴더 최상단의 _Garbage_ 로 "이동"이며,
이동할 때마다 cleanup_logs 에 (원래 경로, 격리 경로)를 남겨 언제든 되돌릴 수 있게 합니다.
"""
import csv
import html
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import database as db

GARBAGE_FOLDER_NAME = "_Garbage_"
REPORT_NAME_RE = re.compile(r"^cleanup_report_s(\d+)_\d{8}_\d{6}\.(html|csv)$")

ProgressCallback = Callable[[int, str, str], None]


def format_bytes(num_bytes: float) -> str:
    if num_bytes < 1024:
        return f"{int(num_bytes)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num_bytes /= 1024.0
        if num_bytes < 1024.0:
            return f"{num_bytes:,.2f} {unit}"
    return f"{num_bytes:,.2f} PB"


def garbage_dir_for(root_path: str) -> Path:
    return Path(root_path) / GARBAGE_FOLDER_NAME


def _build_garbage_path(src: Path, root: Path, garbage_dir: Path, timestamp_tag: str) -> Path:
    """
    격리 파일명 규칙: 원본파일명__상대경로요약_타임스탬프.확장자
      예) D:\\사진\\여행\\2023\\IMG_001.jpg → _Garbage_\\IMG_001__여행_2023_20260101_120000.jpg
    서로 다른 폴더의 동명 파일이 _Garbage_ 한곳에 모여도 덮어쓰지 않도록 하기 위함입니다.
    """
    try:
        rel_parts = src.parent.relative_to(root).parts
        path_slug = "_".join(rel_parts) if rel_parts else "root"
    except ValueError:
        path_slug = "external"
    safe_slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in path_slug)[:40]
    stem = src.stem[:100]  # 지나치게 긴 파일명으로 경로 길이 제한에 걸리지 않도록 절단

    dest = garbage_dir / f"{stem}__{safe_slug}_{timestamp_tag}{src.suffix}"
    counter = 1
    while dest.exists():  # 그래도 겹치면 일련번호를 붙임
        dest = garbage_dir / f"{stem}__{safe_slug}_{timestamp_tag}_{counter}{src.suffix}"
        counter += 1
    return dest


# -----------------------------------------------------------------------------
# 안전 이동
# -----------------------------------------------------------------------------
def move_to_garbage(session_id: int, progress_callback: Optional[ProgressCallback] = None) -> dict:
    """세션에서 is_selected=1 인 PENDING 항목을 _Garbage_ 로 옮기고 보고서를 생성합니다."""
    progress: ProgressCallback = progress_callback or (lambda pct, msg, cur: None)
    session = db.get_session(session_id)
    if session is None:
        raise ValueError("세션을 찾을 수 없습니다.")

    root = Path(session["root_path"]).resolve()
    garbage_dir = root / GARBAGE_FOLDER_NAME
    garbage_dir.mkdir(parents=True, exist_ok=True)

    items = db.get_selected_pending_items(session_id)
    timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    moved: List[dict] = []
    failed: List[dict] = []
    total = len(items)

    for idx, item in enumerate(items, start=1):
        src = Path(item["file_path"])
        progress(int(idx / max(1, total) * 90), f"안전 격리 이동 중 ({idx:,}/{total:,})", src.name)

        # --- 이동 전 안전 검사 -------------------------------------------------
        try:
            resolved = src.resolve()
            inside_root = root == resolved or root in resolved.parents
        except OSError:
            inside_root = False
        if not inside_root:
            failed.append({"path": str(src), "error": "스캔 대상 폴더 밖의 경로"})
            continue
        if not src.is_file():
            failed.append({"path": str(src), "error": "파일이 이미 존재하지 않음"})
            continue
        try:
            current_size = src.stat().st_size
        except OSError as exc:
            failed.append({"path": str(src), "error": str(exc)})
            continue
        if current_size != item["file_size"]:
            # 분석 이후 내용이 바뀐 파일은 더 이상 '중복'이라고 장담할 수 없으므로 건드리지 않음
            failed.append({"path": str(src), "error": "분석 이후 파일이 변경됨 (건너뜀)"})
            continue

        dest = _build_garbage_path(src, root, garbage_dir, timestamp_tag)
        try:
            shutil.move(str(src), str(dest))
        except Exception as exc:
            failed.append({"path": str(src), "error": str(exc)})
            continue

        # 파일 이동 직후 곧바로 DB 에 기록 (이력 INSERT + 항목 상태 MOVED 가 한 트랜잭션)
        db.record_move(session_id, item, str(dest))
        moved.append(
            {
                "file_name": item["file_name"],
                "original_path": item["file_path"],
                "moved_path": str(dest),
                "size_bytes": item["file_size"],
                "reason": item["match_reason"],
            }
        )

    progress(94, "보고서 생성 중...", "")
    saved_bytes = sum(r["size_bytes"] for r in moved)
    reports: List[str] = []
    if moved or failed:
        base = f"cleanup_report_s{session_id}_{timestamp_tag}"
        _write_csv_report(garbage_dir / f"{base}.csv", moved)
        _write_html_report(
            html_path=garbage_dir / f"{base}.html",
            root_dir=str(root),
            garbage_dir=str(garbage_dir),
            timestamp_str=db.now_str(),
            moved=moved,
            failed=failed,
            saved_bytes=saved_bytes,
        )
        reports = [f"{base}.html", f"{base}.csv"]

    progress(100, "파일 정리 및 보고서 생성 완료!", "")
    return {
        "session_id": session_id,
        "moved_count": len(moved),
        "failed_count": len(failed),
        "failed": failed[:50],
        "saved_bytes": saved_bytes,
        "garbage_dir": str(garbage_dir),
        "reports": reports,
    }


# -----------------------------------------------------------------------------
# 원위치 복구
# -----------------------------------------------------------------------------
def restore_files(
    log_ids: Optional[Iterable[int]] = None,
    session_id: Optional[int] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> dict:
    """
    cleanup_logs 에서 아직 복구되지 않은(restored_at IS NULL) 이력을 찾아 원래 경로로 되돌립니다.
      - log_ids 지정   : 해당 이력만 (개별 복구)
      - session_id 지정: 해당 세션 전체
      - 둘 다 없음     : 격리 중인 모든 파일 (일괄 복구)
    원래 자리에 이미 다른 파일이 있으면 덮어쓰지 않고 '이름 (복구본 시각).확장자' 로 복구합니다.
    """
    progress: ProgressCallback = progress_callback or (lambda pct, msg, cur: None)
    logs = db.get_active_logs(log_ids=log_ids, session_id=session_id)
    total = len(logs)
    restored = 0
    renamed: List[dict] = []
    failed: List[dict] = []

    for idx, log in enumerate(logs, start=1):
        src = Path(log["garbage_path"])
        dest = Path(log["original_path"])
        progress(int(idx / max(1, total) * 98), f"원위치 복구 중 ({idx:,}/{total:,})", log["file_name"])

        if not src.is_file():
            failed.append({"path": str(src), "error": "격리 폴더에 파일이 없음"})
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)  # 원래 폴더가 지워졌다면 다시 생성
            if dest.exists():
                tag = datetime.now().strftime("%Y%m%d_%H%M%S")
                alt = dest.with_name(f"{dest.stem} (복구본 {tag}){dest.suffix}")
                counter = 1
                while alt.exists():
                    alt = dest.with_name(f"{dest.stem} (복구본 {tag}_{counter}){dest.suffix}")
                    counter += 1
                renamed.append({"original": str(dest), "restored_as": str(alt)})
                dest = alt
            shutil.move(str(src), str(dest))
        except Exception as exc:
            failed.append({"path": str(src), "error": str(exc)})
            continue

        # 복구 성공 → restored_at 기록 + 항목 상태 RESTORED
        db.record_restore(log["id"], log["item_id"])
        restored += 1

    progress(100, "복구 완료!", "")
    return {
        "requested": total,
        "restored_count": restored,
        "renamed": renamed[:50],
        "failed_count": len(failed),
        "failed": failed[:50],
    }


# -----------------------------------------------------------------------------
# 보고서
# -----------------------------------------------------------------------------
def list_reports(session: dict) -> List[str]:
    """세션의 _Garbage_ 폴더에서 이 세션이 만든 보고서 파일명을 찾습니다."""
    gdir = garbage_dir_for(session["root_path"])
    try:
        names = [p.name for p in gdir.glob(f"cleanup_report_s{session['id']}_*") if p.is_file()]
    except OSError:
        return []
    return sorted(n for n in names if REPORT_NAME_RE.match(n))


def resolve_report(session: dict, name: str) -> Optional[Path]:
    """보고서 다운로드용 경로 확인. 파일명 형식과 세션 번호가 맞을 때만 경로를 돌려줍니다."""
    m = REPORT_NAME_RE.match(name)
    if not m or int(m.group(1)) != session["id"]:
        return None
    path = garbage_dir_for(session["root_path"]) / name
    return path if path.is_file() else None


def _write_csv_report(csv_path: Path, moved: List[dict]) -> None:
    # utf-8-sig(BOM): 엑셀에서 바로 열어도 한글이 깨지지 않도록
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as cf:
        writer = csv.writer(cf)
        writer.writerow(["번호", "파일명", "용량", "용량(bytes)", "유사 판단 사유", "원본 경로", "이동된 경로(_Garbage_)"])
        for i, rec in enumerate(moved, start=1):
            writer.writerow(
                [
                    i,
                    rec["file_name"],
                    format_bytes(rec["size_bytes"]),
                    rec["size_bytes"],
                    rec["reason"],
                    rec["original_path"],
                    rec["moved_path"],
                ]
            )


def _write_html_report(
    html_path: Path,
    root_dir: str,
    garbage_dir: str,
    timestamp_str: str,
    moved: List[dict],
    failed: List[dict],
    saved_bytes: int,
) -> None:
    rows_html = "".join(
        f"<tr><td>{idx}</td>"
        f"<td><strong>{html.escape(rec['file_name'])}</strong></td>"
        f"<td>{html.escape(format_bytes(rec['size_bytes']))}</td>"
        f"<td><span class='badge'>{html.escape(rec['reason'])}</span></td>"
        f"<td class='path'>{html.escape(rec['original_path'])}</td>"
        f"<td class='path moved'>{html.escape(rec['moved_path'])}</td></tr>"
        for idx, rec in enumerate(moved, start=1)
    )
    failed_html = ""
    if failed:
        failed_rows = "".join(
            f"<tr><td class='path'>{html.escape(f['path'])}</td><td>{html.escape(f['error'])}</td></tr>"
            for f in failed
        )
        failed_html = (
            "<h2>건너뛴 파일</h2><table><thead><tr><th>경로</th><th>사유</th></tr></thead>"
            f"<tbody>{failed_rows}</tbody></table>"
        )

    content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>스마트 중복 파일 정리 작업 보고서</title>
<style>
  body {{ font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; background: #f4f6f9; color: #1e293b; margin: 0; padding: 32px; }}
  .container {{ max-width: 1360px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,.06); padding: 32px; }}
  h1 {{ margin-top: 0; color: #0f172a; font-size: 24px; border-bottom: 2px solid #e2e8f0; padding-bottom: 14px; }}
  h2 {{ font-size: 16px; margin: 32px 0 12px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 24px; line-height: 1.7; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
  .card {{ flex: 1; min-width: 220px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 22px; }}
  .card .label {{ font-size: 13px; color: #64748b; margin-bottom: 6px; }}
  .card .value {{ font-size: 26px; font-weight: 700; color: #2563eb; }}
  .card .sub {{ font-size: 12px; color: #94a3b8; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #1e293b; color: #fff; text-align: left; padding: 12px 10px; }}
  td {{ padding: 10px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
  tr:hover {{ background: #f1f5f9; }}
  .path {{ font-family: Consolas, monospace; font-size: 12px; word-break: break-all; color: #475569; }}
  .moved {{ color: #0d9488; }}
  .badge {{ display: inline-block; background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; border-radius: 6px; padding: 2px 8px; font-size: 12px; }}
</style>
</head>
<body>
<div class="container">
  <h1>스마트 중복 파일 정리 작업 보고서</h1>
  <div class="meta">
    <strong>작업 일시:</strong> {html.escape(timestamp_str)}<br>
    <strong>스캔 대상 경로:</strong> {html.escape(root_dir)}<br>
    <strong>안전 격리(_Garbage_) 폴더:</strong> {html.escape(garbage_dir)}
  </div>
  <div class="cards">
    <div class="card">
      <div class="label">정리(이동)된 파일</div>
      <div class="value">{len(moved):,} 개</div>
      <div class="sub">건너뜀: {len(failed):,} 개</div>
    </div>
    <div class="card">
      <div class="label">확보된 용량</div>
      <div class="value">{html.escape(format_bytes(saved_bytes))}</div>
      <div class="sub">{saved_bytes:,} bytes</div>
    </div>
    <div class="card">
      <div class="label">복구</div>
      <div class="value" style="color:#059669;">복구 가능</div>
      <div class="sub">삭제 없이 _Garbage_ 폴더로 이동됨 · 웹 앱의 '정리 이력 · 복구' 탭에서 원위치 복구</div>
    </div>
  </div>
  <table>
    <thead>
      <tr>
        <th style="width:45px;">No</th>
        <th style="width:180px;">파일명</th>
        <th style="width:95px;">크기</th>
        <th style="width:230px;">유사 판단 사유</th>
        <th>원본 경로</th>
        <th>이동된 경로</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  {failed_html}
</div>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as hf:
        hf.write(content)
