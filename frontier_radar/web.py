"""本地 Web 界面：只读浏览 + 少量写操作（反馈、确认、驳回、回滚）。

刻意使用标准库 http.server：
* 零依赖，clone 下来就能跑；
* 默认只监听 127.0.0.1，不做账号体系；
* 表单 POST 采用同源校验，写操作有白名单。
"""

from __future__ import annotations

import json
import mimetypes
import re
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .db import Database
from .engine import claim_state, list_claim_states, publish_site
from .models import ClaimStatus
from .pipeline import RadarPipeline
from .profile import load_profile
from .render import (
    FEEDBACK_VERDICTS,
    render_about_page,
    render_claims_page,
    render_dashboard,
    render_run_page,
)

MAX_BODY = 64 * 1024
VERDICTS = {key for key, _label in FEEDBACK_VERDICTS}
RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_\-]+$")


def _load_digest(db: Database, run_id: str) -> dict[str, Any] | None:
    for artifact in db.artifacts_for_run(run_id):
        if artifact["kind"] == "json":
            path = Path(artifact["path"])
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
    return None


def serve(data_dir: str | Path = "data", host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> int:
    root = Path(data_dir).resolve()
    db_path = root / "radar.db"
    if not db_path.exists():
        print(f"找不到数据库 {db_path}。先运行：python -m frontier_radar demo")
        return 2
    profile_slug = _profile_slug(db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "FrontierRadar/0.1"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._handle_get()
            except Exception as exc:  # noqa: BLE001 - 本地工具，返回可读错误即可
                self._send_html(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"<h1>服务端错误</h1><pre>{_escape(str(exc))}</pre>",
                    title="错误",
                )

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._handle_post()
            except Exception as exc:  # noqa: BLE001
                self._send_html(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"<h1>操作失败</h1><pre>{_escape(str(exc))}</pre>",
                    title="错误",
                )

        # ---- GET -----------------------------------------------------------
        def _handle_get(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/"):
                return self._api(path)
            if path.startswith("/artifacts/") or path.startswith("/static/"):
                return self._static(path)
            if path == "/" or path == "/index.html":
                return self._home()
            if path == "/claims.html":
                return self._claims()
            if path == "/about.html":
                return self._about()
            if path == "/digest/latest":
                return self._latest_digest()
            if RUN_ID_RE.match(path.strip("/").removesuffix(".html")):
                return self._run(path.strip("/").removesuffix(".html"))
            self._send_html(HTTPStatus.NOT_FOUND, "<h1>404</h1><p>页面不存在。</p>", title="404")

        def _db(self) -> Database:
            return Database(db_path)

        def _task_id(self, db: Database) -> str:
            tasks = db.list_tasks()
            return tasks[0]["task_id"] if tasks else ""

        def _home(self) -> None:
            db = self._db()
            try:
                task_id = self._task_id(db)
                task = db.get_task(task_id)
                runs = db.list_runs(task_id, limit=100)
                claims = list_claim_states(db, task_id)
                html_text = render_dashboard(runs, task, claims[:8])
            finally:
                db.close()
            self._send_html(HTTPStatus.OK, html_text)

        def _claims(self) -> None:
            db = self._db()
            try:
                task_id = self._task_id(db)
                html_text = render_claims_page(list_claim_states(db, task_id), db.get_task(task_id))
            finally:
                db.close()
            self._send_html(HTTPStatus.OK, html_text)

        def _about(self) -> None:
            db = self._db()
            try:
                task_id = self._task_id(db)
                html_text = render_about_page(db.get_task(task_id), profile_slug)
            finally:
                db.close()
            self._send_html(HTTPStatus.OK, html_text)

        def _run(self, run_id: str) -> None:
            from .engine import _digest_to_obj, _register_digest_signals

            db = self._db()
            try:
                digest = _load_digest(db, run_id)
                if not digest:
                    return self._send_html(
                        HTTPStatus.NOT_FOUND, "<h1>报告不存在</h1>", title="404"
                    )
                _register_digest_signals(digest)
                html_text = render_run_page(_digest_to_obj(digest))
            finally:
                db.close()
            self._send_html(HTTPStatus.OK, html_text)

        def _latest_digest(self) -> None:
            from .render import render_digest_body
            from .engine import _digest_to_obj, _register_digest_signals

            db = self._db()
            try:
                task_id = self._task_id(db)
                runs = db.list_runs(task_id, limit=1)
                if not runs:
                    return self._send_html(HTTPStatus.OK, "<p>暂无运行。</p>")
                digest = _load_digest(db, runs[0]["run_id"])
                if not digest:
                    return self._send_html(HTTPStatus.OK, "<p>暂无报告。</p>")
                _register_digest_signals(digest)
                obj = _digest_to_obj(digest)
                self._send_html(HTTPStatus.OK, render_digest_body(obj))
            finally:
                db.close()

        def _api(self, path: str) -> None:
            db = self._db()
            try:
                task_id = self._task_id(db)
                if path == "/api/state":
                    payload = {
                        "task": db.get_task(task_id),
                        "claims": list_claim_states(db, task_id),
                        "runs": db.list_runs(task_id, limit=20),
                    }
                    return self._send_json(HTTPStatus.OK, payload)
                run_id = path.removeprefix("/api/runs/")
                if RUN_ID_RE.match(run_id):
                    digest = _load_digest(db, run_id)
                    if digest is None:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return self._send_json(HTTPStatus.OK, digest)
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown_api"})
            finally:
                db.close()

        def _static(self, path: str) -> None:
            relative = path.removeprefix("/artifacts/").removeprefix("/static/")
            target = (root / "artifacts" / relative).resolve()
            if not str(target).startswith(str((root / "artifacts").resolve())):
                return self._send_html(HTTPStatus.FORBIDDEN, "<h1>403</h1>", title="403")
            if not target.is_file():
                return self._send_html(HTTPStatus.NOT_FOUND, "<h1>404</h1>", title="404")
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---- POST ----------------------------------------------------------
        def _handle_post(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY:
                return self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            form = {key: values[0] for key, values in parse_qs(body).items()}
            origin = self.headers.get("Origin", "")
            if origin and "//" in origin:
                host = origin.split("//", 1)[1]
                if host not in {self.headers.get("Host", ""), f"127.0.0.1:{port}", f"localhost:{port}"}:
                    return self._send_json(HTTPStatus.FORBIDDEN, {"error": "cross_origin"})
            redirect = form.get("redirect", "index.html")
            if not re.fullmatch(r"[A-Za-z0-9_./\-]+", redirect or "") or ".." in redirect:
                redirect = "index.html"

            db = self._db()
            try:
                task_id = self._task_id(db)
                pipeline = RadarPipeline(db, load_profile(_profile_slug(db_path)), artifact_root=root / "artifacts")
                claim_id = form.get("claim_id", "")
                if parsed.path == "/api/feedback":
                    verdict = form.get("verdict", "")
                    if verdict not in VERDICTS:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_verdict"})
                    db.add_feedback(
                        task_id,
                        verdict=verdict,
                        run_id=form.get("run_id", ""),
                        claim_id=claim_id,
                        signal_id=form.get("signal_id", ""),
                        note=form.get("note", "")[:500],
                    )
                    db.add_event(task_id, "feedback", {"verdict": verdict, "claim_id": claim_id, "channel": "web"})
                elif parsed.path == "/api/claims/confirm":
                    pipeline.engine.confirm(claim_id, note=form.get("note", "")[:300])
                elif parsed.path == "/api/claims/reject":
                    pipeline.engine.reject(claim_id, note=form.get("note", "")[:300])
                elif parsed.path == "/api/claims/rollback":
                    pipeline.engine.rollback(claim_id, int(form.get("target_version", "1")), note=form.get("note", "")[:300])
                elif parsed.path == "/api/claims/retire":
                    claim = db.get_claim(claim_id)
                    if not claim:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "claim_not_found"})
                    claim.status = ClaimStatus.RETIRED
                    db.upsert_claim(claim)
                    db.add_event(task_id, "claim_retired", {"claim_id": claim_id, "channel": "web"})
                else:
                    return self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint"})
            finally:
                db.close()

            if parsed.path == "/api/feedback":
                return self._send_html(
                    HTTPStatus.OK,
                    "<h1>反馈已记录</h1><p>感谢纠错。反馈会进入验证指标，并在下一期判断中作为复核线索。</p>"
                    f'<p><a href="/{redirect}">返回报告</a></p>',
                    title="反馈已记录",
                )
            publish_site(root, task_id)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/{redirect}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---- 工具 ----------------------------------------------------------
        def _send_html(self, status: int, body: str, title: str = "前沿雷达") -> None:
            data = body.encode("utf-8")
            if not body.lstrip().lower().startswith("<!doctype"):
                body = (
                    "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                    f"<title>{_escape(title)}</title><style>body{{font-family:system-ui,sans-serif;"
                    "max-width:760px;margin:60px auto;padding:0 20px;line-height:1.7;color:#1e2329}}"
                    "a{color:#0f6b5c}</style></head><body>" + body + "</body></html>"
                )
                data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
            if "/api/" in (args[0] if args else ""):
                return
            print(f"  · {self.address_string()} {fmt % args}")

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print("前沿雷达已启动：", url)
    print(f"数据目录：{root}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


def _profile_slug(db_path: Path) -> str:
    db = Database(db_path)
    try:
        tasks = db.list_tasks()
        return str(tasks[0].get("profile_slug") or "solid_state_battery") if tasks else "solid_state_battery"
    finally:
        db.close()


def _escape(text: str) -> str:
    from html import escape

    return escape(text, quote=True)


__all__ = ["serve"]
