# database.py
"""
SQLite 영속 계층 (smart_organizer.db)

테이블 4개로 "스캔 → 검토(선택) → 격리 이동 → 복구" 전 과정을 기록합니다.

  scan_sessions    : 스캔 작업 1회 = 1행
  duplicate_groups : 세션에서 발견된 중복/유사 그룹
  duplicate_items  : 그룹에 속한 개별 파일 + 정리 대상 선택 여부 + 처리 상태
  cleanup_logs     : _Garbage_ 로 옮긴 이력. restored_at 이 NULL 이면 아직 격리 중

연결 정책
  - Flask 요청 스레드와 백그라운드 작업 스레드가 동시에 접근하므로
    연결을 공유하지 않고 호출마다 새로 열고 닫습니다.
  - WAL 저널 모드를 사용해 "읽기(진행률/목록 조회)"와 "쓰기(분석 결과 저장)"가
    서로를 막지 않게 합니다.
  - 쓰기는 모두 transaction() 컨텍스트 안에서 수행하며, 예외가 나면 통째로 롤백합니다.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

DB_PATH = Path(__file__).resolve().parent / "smart_organizer.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_sessions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path              TEXT    NOT NULL,              -- 스캔 대상 폴더
    config_json            TEXT    NOT NULL DEFAULT '{}', -- 분석 옵션(JSON 직렬화)
    status                 TEXT    NOT NULL DEFAULT 'RUNNING'
                           CHECK (status IN ('RUNNING','COMPLETED','CANCELLED','FAILED')),
    total_files            INTEGER NOT NULL DEFAULT 0,
    duplicate_groups_count INTEGER NOT NULL DEFAULT 0,
    reclaimable_bytes      INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES scan_sessions(id) ON DELETE CASCADE,
    group_number      INTEGER NOT NULL,                   -- 화면에 표시되는 Group N
    reclaimable_bytes INTEGER NOT NULL DEFAULT 0          -- 가장 큰 파일 1개만 남길 때 확보되는 용량
);

CREATE TABLE IF NOT EXISTS duplicate_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    file_path        TEXT    NOT NULL,
    file_name        TEXT    NOT NULL,
    ext              TEXT    NOT NULL DEFAULT '',
    file_size        INTEGER NOT NULL DEFAULT 0,
    ctime            REAL    NOT NULL DEFAULT 0,
    mtime            REAL    NOT NULL DEFAULT 0,
    folder_depth     INTEGER NOT NULL DEFAULT 0,
    is_image         BOOLEAN NOT NULL DEFAULT 0,
    match_reason     TEXT    NOT NULL DEFAULT '',
    confidence_score REAL    NOT NULL DEFAULT 0,
    is_selected      BOOLEAN NOT NULL DEFAULT 0,          -- 1 = 정리(_Garbage_ 이동) 대상
    status           TEXT    NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','MOVED','RESTORED'))
);

CREATE TABLE IF NOT EXISTS cleanup_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    INTEGER NOT NULL REFERENCES scan_sessions(id) ON DELETE CASCADE,
    item_id       INTEGER REFERENCES duplicate_items(id) ON DELETE SET NULL,
    file_name     TEXT    NOT NULL,
    original_path TEXT    NOT NULL,                       -- 복구 시 되돌아갈 위치
    garbage_path  TEXT    NOT NULL,                       -- _Garbage_ 안의 현재 위치
    file_size     INTEGER NOT NULL DEFAULT 0,
    match_reason  TEXT    NOT NULL DEFAULT '',
    moved_at      TEXT    NOT NULL,
    restored_at   TEXT                                    -- NULL 이면 현재 격리 중
);

CREATE INDEX IF NOT EXISTS idx_groups_session ON duplicate_groups(session_id);
CREATE INDEX IF NOT EXISTS idx_items_group    ON duplicate_items(group_id);
CREATE INDEX IF NOT EXISTS idx_items_path     ON duplicate_items(file_path);
CREATE INDEX IF NOT EXISTS idx_logs_session   ON cleanup_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_logs_garbage   ON cleanup_logs(garbage_path);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_connection() -> sqlite3.Connection:
    """호출마다 새 연결을 엽니다. isolation_level=None 으로 두고 트랜잭션을 직접 제어합니다."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # FK 는 연결마다 켜야 적용됩니다.
    return conn


@contextmanager
def transaction():
    """
    쓰기 트랜잭션 헬퍼.
    BEGIN IMMEDIATE 로 시작해 쓰기 잠금을 먼저 확보하므로, 두 스레드가 동시에 쓰려 해도
    중간에 'database is locked' 로 깨지지 않고 timeout(30초) 동안 순서를 기다립니다.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _query(sql: str, params: Iterable = ()) -> List[dict]:
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def init_db() -> None:
    """DB 파일과 스키마를 만들고, 비정상 종료로 남은 RUNNING 세션을 FAILED 로 정리합니다."""
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    with transaction() as conn:
        # 서버가 분석 도중 꺼졌다면 그 세션은 영원히 RUNNING 으로 남으므로 시작 시 닫아 둡니다.
        conn.execute("UPDATE scan_sessions SET status = 'FAILED' WHERE status = 'RUNNING'")


# -----------------------------------------------------------------------------
# scan_sessions
# -----------------------------------------------------------------------------
def create_session(root_path: str, config: dict) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO scan_sessions (root_path, config_json, status, created_at) VALUES (?, ?, 'RUNNING', ?)",
            (root_path, json.dumps(config, ensure_ascii=False), now_str()),
        )
        return cur.lastrowid


def set_session_status(session_id: int, status: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE scan_sessions SET status = ? WHERE id = ?", (status, session_id))


def get_session(session_id: int) -> Optional[dict]:
    rows = _query("SELECT * FROM scan_sessions WHERE id = ?", (session_id,))
    return rows[0] if rows else None


# -----------------------------------------------------------------------------
# 분석 결과 저장 / 조회
# -----------------------------------------------------------------------------
def save_analysis_results(session_id: int, groups: list, total_files: int) -> dict:
    """
    analyzer 가 만든 DuplicateGroup 목록을 한 트랜잭션으로 저장합니다.
    그룹/항목 INSERT 와 세션 상태 COMPLETED 갱신이 함께 커밋되므로,
    "세션은 완료인데 그룹이 절반만 들어간" 상태가 생기지 않습니다.

    기본 선택값: 그룹마다 수정일이 가장 최신인 파일 1개를 보존(is_selected=0)하고
    나머지를 정리 대상(is_selected=1)으로 미리 체크해 둡니다.
    """
    total_reclaimable = 0
    with transaction() as conn:
        for grp in groups:
            reclaimable = grp.reclaimable_size
            total_reclaimable += reclaimable
            cur = conn.execute(
                "INSERT INTO duplicate_groups (session_id, group_number, reclaimable_bytes) VALUES (?, ?, ?)",
                (session_id, grp.group_id, reclaimable),
            )
            group_pk = cur.lastrowid

            # 보존할 1개: 수정일 최신 → 같으면 폴더 깊이가 얕은 쪽
            keep = max(grp.items, key=lambda it: (it.record.mtime, -it.record.depth))
            conn.executemany(
                """INSERT INTO duplicate_items
                   (group_id, file_path, file_name, ext, file_size, ctime, mtime, folder_depth,
                    is_image, match_reason, confidence_score, is_selected, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
                [
                    (
                        group_pk,
                        it.record.path,
                        it.record.name,
                        it.record.ext,
                        it.record.size,
                        it.record.ctime,
                        it.record.mtime,
                        it.record.depth,
                        1 if it.record.is_image else 0,
                        it.match_reason,
                        round(it.confidence_score, 2),
                        0 if it is keep else 1,
                    )
                    for it in grp.items
                ],
            )

        conn.execute(
            """UPDATE scan_sessions
               SET status = 'COMPLETED', total_files = ?, duplicate_groups_count = ?, reclaimable_bytes = ?
               WHERE id = ?""",
            (total_files, len(groups), total_reclaimable, session_id),
        )
    return {
        "total_files": total_files,
        "duplicate_groups_count": len(groups),
        "reclaimable_bytes": total_reclaimable,
    }


def get_groups(session_id: int, pending_only: bool = True) -> List[dict]:
    """
    세션의 그룹과 항목을 중첩 구조로 반환합니다.
    pending_only=True 이면 아직 처리되지 않은(PENDING) 항목만 싣고,
    남은 항목이 2개 미만인 그룹은 더 이상 '중복'이 아니므로 제외합니다.
    """
    groups = _query(
        "SELECT id, group_number, reclaimable_bytes FROM duplicate_groups WHERE session_id = ? ORDER BY group_number",
        (session_id,),
    )
    sql = """SELECT i.* FROM duplicate_items i
             JOIN duplicate_groups g ON g.id = i.group_id
             WHERE g.session_id = ?"""
    if pending_only:
        sql += " AND i.status = 'PENDING'"
    sql += " ORDER BY i.group_id, i.mtime DESC"
    by_group: dict = {}
    for item in _query(sql, (session_id,)):
        item["is_image"] = bool(item["is_image"])
        item["is_selected"] = bool(item["is_selected"])
        by_group.setdefault(item["group_id"], []).append(item)

    result = []
    for g in groups:
        items = by_group.get(g["id"], [])
        if pending_only and len(items) < 2:
            continue
        g["items"] = items
        result.append(g)
    return result


def set_selection(session_id: int, selected_ids: Iterable[int]) -> int:
    """
    세션의 정리 대상 선택 상태를 통째로 교체합니다.
    (1) 세션의 PENDING 항목을 모두 해제하고 (2) 전달된 id 만 다시 체크합니다.
    두 단계가 한 트랜잭션이므로 도중에 실패해도 이전 선택 상태가 그대로 남습니다.
    WHERE 절에 session_id 조건을 함께 걸어 다른 세션의 항목 id 가 섞여 와도 무시됩니다.
    """
    ids = [(int(i), session_id) for i in selected_ids]
    with transaction() as conn:
        conn.execute(
            """UPDATE duplicate_items SET is_selected = 0
               WHERE status = 'PENDING'
                 AND group_id IN (SELECT id FROM duplicate_groups WHERE session_id = ?)""",
            (session_id,),
        )
        conn.executemany(
            """UPDATE duplicate_items SET is_selected = 1
               WHERE id = ? AND status = 'PENDING'
                 AND group_id IN (SELECT id FROM duplicate_groups WHERE session_id = ?)""",
            ids,
        )
    return len(ids)


def get_selected_pending_items(session_id: int) -> List[dict]:
    return _query(
        """SELECT i.* FROM duplicate_items i
           JOIN duplicate_groups g ON g.id = i.group_id
           WHERE g.session_id = ? AND i.is_selected = 1 AND i.status = 'PENDING'
           ORDER BY g.group_number, i.id""",
        (session_id,),
    )


# -----------------------------------------------------------------------------
# cleanup_logs (격리 이동 / 복구 이력)
# -----------------------------------------------------------------------------
def record_move(session_id: int, item: dict, garbage_path: str) -> int:
    """
    파일 1개를 _Garbage_ 로 옮긴 직후 호출합니다.
    이력 INSERT 와 항목 상태 MOVED 갱신을 한 트랜잭션으로 묶어,
    "파일은 옮겨졌는데 복구 이력이 없는" 상태를 만들지 않습니다.
    (파일마다 즉시 커밋하므로 작업 도중 서버가 꺼져도 그때까지 옮긴 파일은 모두 복구 가능합니다.)
    """
    with transaction() as conn:
        cur = conn.execute(
            """INSERT INTO cleanup_logs
               (session_id, item_id, file_name, original_path, garbage_path, file_size, match_reason, moved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                item["id"],
                item["file_name"],
                item["file_path"],
                garbage_path,
                item["file_size"],
                item["match_reason"],
                now_str(),
            ),
        )
        conn.execute("UPDATE duplicate_items SET status = 'MOVED' WHERE id = ?", (item["id"],))
        return cur.lastrowid


def get_active_logs(log_ids: Optional[Iterable[int]] = None, session_id: Optional[int] = None) -> List[dict]:
    """아직 복구되지 않은(restored_at IS NULL) 이력을 조회합니다. 조건이 없으면 전체."""
    sql = "SELECT * FROM cleanup_logs WHERE restored_at IS NULL"
    params: list = []
    if session_id is not None:
        sql += " AND session_id = ?"
        params.append(session_id)
    rows = _query(sql + " ORDER BY id", params)
    if log_ids is not None:
        wanted = {int(i) for i in log_ids}
        rows = [r for r in rows if r["id"] in wanted]
    return rows


def record_restore(log_id: int, item_id: Optional[int]) -> None:
    """복구 완료 기록: 이력에 restored_at 을 찍고 항목 상태를 RESTORED 로 바꿉니다."""
    with transaction() as conn:
        conn.execute("UPDATE cleanup_logs SET restored_at = ? WHERE id = ?", (now_str(), log_id))
        if item_id is not None:
            conn.execute("UPDATE duplicate_items SET status = 'RESTORED' WHERE id = ?", (item_id,))


def get_logs(session_id: int) -> List[dict]:
    return _query("SELECT * FROM cleanup_logs WHERE session_id = ? ORDER BY id", (session_id,))


def list_history(limit: int = 200) -> dict:
    """정리 이력 탭용: 세션 목록(세션별 이동/격리/복구 집계 포함)과 전체 통계."""
    sessions = _query(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM cleanup_logs l WHERE l.session_id = s.id) AS moved_count,
                  (SELECT COALESCE(SUM(l.file_size), 0) FROM cleanup_logs l WHERE l.session_id = s.id) AS moved_bytes,
                  (SELECT COUNT(*) FROM cleanup_logs l
                    WHERE l.session_id = s.id AND l.restored_at IS NULL) AS active_count,
                  (SELECT COALESCE(SUM(l.file_size), 0) FROM cleanup_logs l
                    WHERE l.session_id = s.id AND l.restored_at IS NULL) AS active_bytes,
                  -- 아직 검토할 거리가 남은 그룹 수 = 미처리(PENDING) 항목이 2개 이상인 그룹
                  (SELECT COUNT(*) FROM (
                      SELECT g.id FROM duplicate_groups g
                        JOIN duplicate_items i ON i.group_id = g.id AND i.status = 'PENDING'
                       WHERE g.session_id = s.id
                       GROUP BY g.id HAVING COUNT(*) >= 2)) AS reviewable_groups
           FROM scan_sessions s
           ORDER BY s.id DESC
           LIMIT ?""",
        (limit,),
    )
    totals = _query(
        """SELECT COUNT(DISTINCT session_id) AS cleanup_sessions,
                  COUNT(*) AS moved_count,
                  COALESCE(SUM(file_size), 0) AS moved_bytes,
                  COALESCE(SUM(CASE WHEN restored_at IS NULL THEN 1 ELSE 0 END), 0) AS active_count,
                  COALESCE(SUM(CASE WHEN restored_at IS NULL THEN file_size ELSE 0 END), 0) AS active_bytes,
                  COALESCE(SUM(CASE WHEN restored_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS restored_count
           FROM cleanup_logs"""
    )[0]
    return {"sessions": sessions, "totals": totals}


def is_known_path(path: str) -> bool:
    """
    미리보기/파일 열기 API 의 접근 허용 목록.
    분석 결과에 등장한 파일이거나 _Garbage_ 로 옮겨 둔 파일일 때만 True 입니다.
    (임의 경로를 넘겨 PC 의 아무 파일이나 읽고 실행하는 것을 막기 위한 검사)
    """
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM duplicate_items WHERE file_path = ? LIMIT 1", (path,)).fetchone():
            return True
        if conn.execute("SELECT 1 FROM cleanup_logs WHERE garbage_path = ? LIMIT 1", (path,)).fetchone():
            return True
        return False
    finally:
        conn.close()
