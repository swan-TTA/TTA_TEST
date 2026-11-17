# app.py
"""
Flask 메인 서버 — REST API + 백그라운드 작업 스레드 관리

오래 걸리는 작업(분석 / _Garbage_ 이동 / 복구)은 threading.Thread 로 돌리고,
진행 상황을 메모리(TASKS)에 적어 두면 브라우저가 GET /api/progress/<task_id> 로 폴링합니다.

이 서버는 로컬 파일을 읽고 옮기는 도구이므로 127.0.0.1 에만 바인딩하며,
파일 미리보기/열기 API 는 DB 에 기록된 경로(분석 결과·격리 파일)만 허용합니다.
"""
import io
import os
import string
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

import database as db
import organizer
from analyzer import IMAGE_EXTENSIONS, AnalysisConfig, DuplicateAnalyzer

HOST = "127.0.0.1"
PORT = 5000

app = Flask(__name__)
app.json.ensure_ascii = False  # 한글 경로/메시지를 \uXXXX 로 바꾸지 않고 그대로 전송

# -----------------------------------------------------------------------------
# 백그라운드 작업 상태 저장소 (메모리)
# -----------------------------------------------------------------------------
TASKS: dict = {}       # task_id -> 진행 상태(dict, JSON 직렬화 가능한 값만)
ANALYZERS: dict = {}   # task_id -> 실행 중인 DuplicateAnalyzer (중지 요청 전달용)
TASKS_LOCK = threading.Lock()


def _new_task(kind: str, session_id=None) -> str:
    task_id = uuid.uuid4().hex
    with TASKS_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "kind": kind,                # scan / cleanup / restore
            "status": "RUNNING",         # RUNNING / COMPLETED / CANCELLED / FAILED
            "percent": 0,
            "message": "작업 준비 중...",
            "current_file": "",
            "session_id": session_id,
            "result": None,
            "error": None,
        }
    return task_id


def _update_task(task_id: str, **fields) -> None:
    with TASKS_LOCK:
        if task_id in TASKS:
            TASKS[task_id].update(fields)


def _progress_cb(task_id: str, cap: int = 100):
    """작업 스레드가 호출할 진행률 콜백. cap 으로 '마무리 단계 전까지의 최대 %'를 제한합니다."""
    def callback(percent: int, message: str, current_file: str = "") -> None:
        _update_task(task_id, percent=min(int(percent), cap), message=message, current_file=current_file)
    return callback


def _start_thread(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


# -----------------------------------------------------------------------------
# 작업 스레드 본체
# -----------------------------------------------------------------------------
def _run_scan(task_id: str, session_id: int, root_path: str, config: AnalysisConfig) -> None:
    analyzer = DuplicateAnalyzer(config)
    ANALYZERS[task_id] = analyzer
    try:
        # 분석이 100% 를 보고해도 DB 저장이 끝나기 전까지는 96% 로 표시
        groups = analyzer.scan_and_analyze(root_path, _progress_cb(task_id, cap=96))
        if analyzer.cancelled:
            db.set_session_status(session_id, "CANCELLED")
            _update_task(task_id, status="CANCELLED", message="분석이 중지되었습니다.", current_file="")
            return

        _update_task(task_id, percent=97, message="분석 결과를 데이터베이스에 저장 중...", current_file="")
        summary = db.save_analysis_results(session_id, groups, analyzer.total_files)
        _update_task(
            task_id,
            status="COMPLETED",
            percent=100,
            message=f"분석 완료! 중복/유사 그룹 {summary['duplicate_groups_count']:,}개 발견",
            current_file="",
            result=summary,
        )
    except Exception as exc:  # 스레드 안의 예외는 화면에 보이지 않으므로 반드시 상태로 전달
        db.set_session_status(session_id, "FAILED")
        _update_task(task_id, status="FAILED", error=str(exc), message="분석 중 오류가 발생했습니다.")
    finally:
        ANALYZERS.pop(task_id, None)


def _run_cleanup(task_id: str, session_id: int) -> None:
    try:
        result = organizer.move_to_garbage(session_id, _progress_cb(task_id, cap=99))
        _update_task(task_id, status="COMPLETED", percent=100, message="파일 정리 완료!", current_file="", result=result)
    except Exception as exc:
        _update_task(task_id, status="FAILED", error=str(exc), message="파일 정리 중 오류가 발생했습니다.")


def _run_restore(task_id: str, log_ids, session_id) -> None:
    try:
        result = organizer.restore_files(log_ids=log_ids, session_id=session_id, progress_callback=_progress_cb(task_id, cap=99))
        _update_task(task_id, status="COMPLETED", percent=100, message="복구 완료!", current_file="", result=result)
    except Exception as exc:
        _update_task(task_id, status="FAILED", error=str(exc), message="복구 중 오류가 발생했습니다.")


# -----------------------------------------------------------------------------
# 공통 헬퍼
# -----------------------------------------------------------------------------
def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _parse_config(raw: dict) -> AnalysisConfig:
    """화면의 튜닝 패널 값(JSON)을 검증·보정해 AnalysisConfig 로 변환"""
    return AnalysisConfig(
        include_subfolders=bool(raw.get("include_subfolders", True)),
        min_file_size_bytes=int(_clamp(raw.get("min_size_kb"), 0, 1024 * 1024 * 100, 1) * 1024),
        size_tolerance_percent=_clamp(raw.get("size_tolerance_percent"), 0, 50, 5.0),
        max_time_diff_hours=_clamp(raw.get("max_time_diff_hours"), 0, 24 * 365 * 50, 0.0),
        filename_similarity_min=_clamp(raw.get("name_similarity_percent"), 30, 100, 65) / 100.0,
        enable_image_phash=bool(raw.get("enable_image_phash", True)),
        strict_hex_verification=bool(raw.get("strict_hex_verification", True)),
        garbage_folder_name=organizer.GARBAGE_FOLDER_NAME,
    )


@app.before_request
def _only_local_requests():
    """localhost 로 들어온 요청만 처리 (다른 도메인 이름으로 우회 접속하는 DNS 리바인딩 차단)"""
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        abort(403)
    # 상태를 바꾸는 요청은 JSON 본문만 허용 → 외부 웹페이지의 단순 form 전송으로는 호출 불가
    if request.method == "POST" and not request.is_json:
        abort(415)


# -----------------------------------------------------------------------------
# 페이지
# -----------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", home=str(Path.home()))


# -----------------------------------------------------------------------------
# 로컬 폴더 탐색기 API
# -----------------------------------------------------------------------------
@app.get("/api/browse-dirs")
def browse_dirs():
    """
    서버(=내 PC) 쪽 디렉터리 트리를 한 단계씩 탐색합니다.
    브라우저의 <input type="file"> 은 보안상 전체 경로를 알려 주지 않으므로 이 API 로 폴더를 고릅니다.
    """
    raw = (request.args.get("path") or "").strip()
    path = Path(raw).expanduser() if raw else Path.home()
    try:
        path = path.resolve()
    except OSError:
        return _error("경로를 해석할 수 없습니다.")
    if not path.is_dir():
        return _error("존재하지 않는 폴더입니다.", 404)

    dirs = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir() and not entry.name.startswith((".", "$")):
                        dirs.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except PermissionError:
        return _error("이 폴더에 접근할 권한이 없습니다.", 403)
    except OSError as exc:
        return _error(str(exc))
    dirs.sort(key=lambda d: d["name"].lower())

    # 바로가기: 홈/바탕화면/문서/다운로드 + (Windows) 드라이브 목록
    home = Path.home()
    shortcuts = [{"name": "홈", "path": str(home)}]
    for label, folder in (("바탕화면", "Desktop"), ("문서", "Documents"), ("다운로드", "Downloads"), ("사진", "Pictures")):
        if (home / folder).is_dir():
            shortcuts.append({"name": label, "path": str(home / folder)})
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                shortcuts.append({"name": f"{letter}:", "path": drive})
    else:
        shortcuts.append({"name": "/", "path": "/"})

    parent = str(path.parent) if path.parent != path else None
    return jsonify({"path": str(path), "parent": parent, "dirs": dirs, "shortcuts": shortcuts})


# -----------------------------------------------------------------------------
# 분석(스캔)
# -----------------------------------------------------------------------------
@app.post("/api/scan")
def start_scan():
    body = _json_body()
    raw_path = str(body.get("root_path") or "").strip().strip('"')
    if not raw_path:
        return _error("정리 대상 폴더 경로를 입력해 주세요.")
    try:
        root = Path(raw_path).expanduser().resolve()
    except OSError:
        return _error("경로를 해석할 수 없습니다.")
    if not root.is_dir():
        return _error("유효한 폴더 경로가 아닙니다.")

    config = _parse_config(body.get("config") or {})
    session_id = db.create_session(str(root), asdict(config))
    task_id = _new_task("scan", session_id)
    _start_thread(_run_scan, task_id, session_id, str(root), config)
    return jsonify({"task_id": task_id, "session_id": session_id, "root_path": str(root)})


@app.get("/api/progress/<task_id>")
def get_progress(task_id: str):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return _error("작업을 찾을 수 없습니다.", 404)
        return jsonify(dict(task))


@app.post("/api/cancel/<task_id>")
def cancel_task(task_id: str):
    analyzer = ANALYZERS.get(task_id)
    if analyzer is None:
        return _error("중지할 수 있는 분석 작업이 없습니다.", 404)
    analyzer.cancel()
    _update_task(task_id, message="분석 중지 요청 중...")
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# 분석 결과 조회 / 선택 상태 저장
# -----------------------------------------------------------------------------
@app.get("/api/sessions/<int:session_id>/groups")
def get_session_groups(session_id: int):
    session = db.get_session(session_id)
    if session is None:
        return _error("세션을 찾을 수 없습니다.", 404)
    return jsonify({"session": session, "groups": db.get_groups(session_id, pending_only=True)})


@app.post("/api/sessions/<int:session_id>/selection")
def save_selection(session_id: int):
    if db.get_session(session_id) is None:
        return _error("세션을 찾을 수 없습니다.", 404)
    ids = _json_body().get("selected_ids")
    if not isinstance(ids, list):
        return _error("selected_ids 목록이 필요합니다.")
    try:
        count = db.set_selection(session_id, ids)
    except (TypeError, ValueError):
        return _error("selected_ids 형식이 올바르지 않습니다.")
    return jsonify({"ok": True, "selected": count})


# -----------------------------------------------------------------------------
# 파일 정리(_Garbage_ 이동) / 복구
# -----------------------------------------------------------------------------
@app.post("/api/cleanup")
def start_cleanup():
    body = _json_body()
    try:
        session_id = int(body.get("session_id"))
    except (TypeError, ValueError):
        return _error("session_id 가 필요합니다.")
    session = db.get_session(session_id)
    if session is None:
        return _error("세션을 찾을 수 없습니다.", 404)
    if session["status"] != "COMPLETED":
        return _error("분석이 완료된 세션만 정리할 수 있습니다.")

    # 화면의 최종 선택 상태를 함께 받아 DB 에 확정 저장한 뒤 이동을 시작
    ids = body.get("selected_ids")
    if isinstance(ids, list):
        try:
            db.set_selection(session_id, ids)
        except (TypeError, ValueError):
            return _error("selected_ids 형식이 올바르지 않습니다.")
    if not db.get_selected_pending_items(session_id):
        return _error("정리할 파일이 선택되지 않았습니다.")

    task_id = _new_task("cleanup", session_id)
    _start_thread(_run_cleanup, task_id, session_id)
    return jsonify({"task_id": task_id, "session_id": session_id})


@app.post("/api/restore")
def start_restore():
    """
    본문 예) {"log_ids": [3, 4]}   → 개별 복구
            {"session_id": 2}     → 세션 전체 복구
            {"all": true}         → 격리 중인 모든 파일 복구
    """
    body = _json_body()
    log_ids = body.get("log_ids")
    session_id = body.get("session_id")
    try:
        log_ids = [int(i) for i in log_ids] if isinstance(log_ids, list) else None
        session_id = int(session_id) if session_id is not None else None
    except (TypeError, ValueError):
        return _error("요청 형식이 올바르지 않습니다.")
    if log_ids is None and session_id is None and not body.get("all"):
        return _error("복구 대상을 지정해 주세요.")
    if not db.get_active_logs(log_ids=log_ids, session_id=session_id):
        return _error("복구할 격리 파일이 없습니다.")

    task_id = _new_task("restore", session_id)
    _start_thread(_run_restore, task_id, log_ids, session_id)
    return jsonify({"task_id": task_id})


# -----------------------------------------------------------------------------
# 정리 이력
# -----------------------------------------------------------------------------
@app.get("/api/history")
def get_history():
    history = db.list_history()
    for session in history["sessions"]:
        session["reports"] = organizer.list_reports(session) if session["moved_count"] else []
    return jsonify(history)


@app.get("/api/sessions/<int:session_id>/logs")
def get_session_logs(session_id: int):
    if db.get_session(session_id) is None:
        return _error("세션을 찾을 수 없습니다.", 404)
    return jsonify({"logs": db.get_logs(session_id)})


@app.get("/api/report-file/<int:session_id>/<name>")
def get_report_file(session_id: int, name: str):
    session = db.get_session(session_id)
    path = organizer.resolve_report(session, name) if session else None
    if path is None:
        abort(404)
    return send_file(path, as_attachment=request.args.get("download") == "1", download_name=name)


# -----------------------------------------------------------------------------
# 파일 미리보기 / OS 기본 프로그램으로 열기
# -----------------------------------------------------------------------------
def _known_file(raw_path) -> Path:
    """DB 에 기록된 경로이고 실제로 존재하는 파일일 때만 Path 를 돌려줍니다."""
    path = str(raw_path or "")
    if not path or not db.is_known_path(path):
        abort(403)
    file_path = Path(path)
    if not file_path.is_file():
        abort(404)
    return file_path


@app.get("/api/preview")
def preview():
    """이미지 미리보기. thumb=1 이면 size(px) 이하로 줄인 썸네일, 아니면 원본을 그대로 전송."""
    file_path = _known_file(request.args.get("path"))
    if file_path.suffix.lower() not in IMAGE_EXTENSIONS:
        abort(415)

    if request.args.get("thumb") != "1":
        return send_file(file_path)

    size = int(_clamp(request.args.get("size"), 32, 1024, 480))
    try:
        with Image.open(file_path) as img:
            img.draft("RGB", (size, size))        # JPEG 는 디코딩 단계에서 미리 축소 → 대용량 사진도 빠름
            img = ImageOps.exif_transpose(img)    # 촬영 방향(EXIF) 반영
            img.thumbnail((size, size))
            buf = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P"):
                img.convert("RGBA").save(buf, "PNG")
                mimetype = "image/png"
            else:
                img.convert("RGB").save(buf, "JPEG", quality=82)
                mimetype = "image/jpeg"
    except Exception:
        abort(415)
    buf.seek(0)
    response = send_file(buf, mimetype=mimetype)
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@app.post("/api/open-local")
def open_local():
    """OS 기본 프로그램으로 파일 열기 (Windows: os.startfile / macOS: open / Linux: xdg-open)"""
    file_path = _known_file(_json_body().get("path"))
    try:
        if os.name == "nt":
            os.startfile(str(file_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(file_path)])
        else:
            subprocess.Popen(["xdg-open", str(file_path)])
    except Exception as exc:
        return _error(f"파일을 열 수 없습니다: {exc}", 500)
    return jsonify({"ok": True})


if __name__ == "__main__":
    db.init_db()
    print(f"\n  스마트 중복 파일 정리기 실행 중 →  http://{HOST}:{PORT}\n  (종료: Ctrl+C)\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
