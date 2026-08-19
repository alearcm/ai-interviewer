"""Web front end: a session manager plus an aiohttp app.

The wire format IS the transcript: every recorded row streams to
connected clients as {"i": index, "row": {...}} over a WebSocket, and
reconnecting clients resume by replaying rows from the JSONL file —
the same file the offline regrade reads. There is no second schema.

The manager is deliberately shaped for many sessions even while one
person uses it: create/list/attach/end. Auth is intentionally absent —
bind to localhost or a private tailnet; put an authenticating proxy in
front before sharing (see docs/DEPLOY.md).

Client actions arrive over the same WebSocket: chat lines, commands,
runs, file saves into the workspace (which flow through the ordinary
watcher, so the engine's observation path is unchanged), and debounced
edit pulses for typing-cadence timing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
from typing import Any, Dict, List, Optional

try:
    from aiohttp import WSMsgType, web
except Exception:  # noqa: BLE001
    web = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]

from . import events
from .adapters import make_adapter
from .chat import Pane
from .pack import PackError, load_pack
from .session import Session

# Row kinds whose payload is not streamed to clients (index is kept so
# resume arithmetic stays exact). The pack snapshot is large and clients
# have no use for it live.
_STUB_KINDS = {events.PACK_SNAPSHOT}

_AUTH_COOKIE = "harness_auth"
_AUTH_MAX_AGE = 180 * 24 * 3600  # one login per device per ~6 months

_LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>log in</title>
<style>
body { background:#11141a; color:#dbe2ea; font:16px system-ui, sans-serif;
       display:flex; align-items:center; justify-content:center; height:100dvh; margin:0; }
form { background:#1a1f27; padding:2rem; border-radius:12px; display:flex;
       flex-direction:column; gap:.9rem; min-width:16rem; }
input { background:#202733; color:#dbe2ea; border:1px solid #303a48;
        border-radius:8px; padding:.6rem .8rem; font-size:1rem; }
button { background:#6ea8fe; color:#0b1220; border:0; border-radius:8px;
         padding:.6rem; font-weight:600; font-size:1rem; }
.err { color:#e05d5d; min-height:1.2em; font-size:.9rem; }
</style></head><body>
<form method="post" action="/login">
  <strong>interview harness</strong>
  <input type="password" name="password" placeholder="password" autofocus autocomplete="current-password">
  <button>log in</button>
  <div class="err">%s</div>
</form></body></html>"""


class WebUnavailable(RuntimeError):
    pass


class WebPane(Pane):
    """A pane that renders nothing to the terminal; transcript rows
    already stream to clients, and ephemeral notices go via a hook."""

    def __init__(self) -> None:
        super().__init__(interactive=False)
        self._notice_hook = None

    def attach(self, hook: Any) -> None:
        self._notice_hook = hook

    def _emit(self, line: str) -> None:  # silence the terminal
        pass

    def notice(self, text: str) -> None:
        if self._notice_hook is not None:
            self._notice_hook(text)


class Handle:
    """One live session as seen by the web layer."""

    def __init__(self, sid: str, session: Session, loop: asyncio.AbstractEventLoop) -> None:
        self.id = sid
        self.session = session
        self.loop = loop
        self.clients: set = set()
        self.rows = 0
        self._lock = threading.Lock()
        session.row_sinks.append(self._on_row)

    # called on the session loop thread
    def _on_row(self, row: Dict[str, Any]) -> None:
        with self._lock:
            idx = self.rows
            self.rows += 1
        out = dict(row)
        if row["kind"] in _STUB_KINDS:
            out = {"t": row["t"], "ts": row.get("ts"), "kind": row["kind"], "stub": True}
        try:
            self.loop.call_soon_threadsafe(self._fanout, {"i": idx, "row": out})
        except RuntimeError:
            pass  # loop already closed

    def notice(self, text: str) -> None:
        try:
            self.loop.call_soon_threadsafe(self._fanout, {"notice": text})
        except RuntimeError:
            pass

    # called on the event loop
    def _fanout(self, payload: Dict[str, Any]) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def meta(self) -> Dict[str, Any]:
        session = self.session
        return {
            "id": self.id,
            "pack": session.pack.name,
            "title": session.pack.title,
            "minutes": session.minutes,
            "over": session.is_over(),
            "rows": self.rows,
            "now_t": session.offset(),
            "has_workspace": session.workspace is not None,
            "primary_file": session.pack.primary_file,
            "run_cmd": session.pack.run_cmd,
            "tasks_total": len(session.task_order),
        }


class Manager:
    def __init__(self, settings: Dict[str, Any]) -> None:
        self.settings = settings
        self.packs_dir = settings["web"]["packs_dir"]
        self.live: Dict[str, Handle] = {}
        self._lock = threading.Lock()

    def list_packs(self) -> List[Dict[str, Any]]:
        out = []
        if not os.path.isdir(self.packs_dir):
            return out
        for name in sorted(os.listdir(self.packs_dir)):
            path = os.path.join(self.packs_dir, name)
            if not os.path.isfile(os.path.join(path, "pack.toml")):
                continue
            try:
                pack = load_pack(path)
            except PackError:
                continue
            out.append(
                {
                    "name": pack.name,
                    "title": pack.title,
                    "minutes": pack.minutes,
                    "workspace": pack.workspace,
                    "tasks": [{"id": t["id"], "title": t["title"]} for t in pack.tasks],
                }
            )
        return out

    def create(
        self,
        loop: asyncio.AbstractEventLoop,
        pack_name: str,
        *,
        minutes: Optional[float] = None,
        task_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Handle:
        running = sum(1 for h in self.live.values() if not h.session.is_over())
        limit = int(self.settings["web"].get("max_live", 8))
        if running >= limit:
            raise RuntimeError(
                "session limit reached (%d live); end one first" % running
            )
        safe = os.path.basename(str(pack_name))
        pack = load_pack(os.path.join(self.packs_dir, safe))
        model_cfg = dict(self.settings["model"])
        if provider:
            model_cfg["provider"] = provider
        adapter = make_adapter(model_cfg, pack.fallback_lines)
        pane = WebPane()
        session = Session(
            pack,
            self.settings,
            adapter,
            pane,
            task_id=task_id or None,
            minutes=minutes,
        )
        handle = Handle(session.session_id, session, loop)
        pane.attach(handle.notice)
        with self._lock:
            self.live[handle.id] = handle
        session.start()
        return handle

    def get(self, sid: str) -> Optional[Handle]:
        return self.live.get(sid)

    def list_sessions(self) -> List[Dict[str, Any]]:
        out = [h.meta() for h in self.live.values()]
        base = self.settings["paths"]["sessions_dir"]
        seen = {h.id for h in self.live.values()}
        if os.path.isdir(base):
            for name in sorted(os.listdir(base), reverse=True):
                if name in seen or name.startswith("_"):
                    continue
                if os.path.isfile(os.path.join(base, name, "transcript.jsonl")):
                    out.append({"id": name, "over": True, "past": True})
        return out

    def shutdown(self) -> None:
        for handle in list(self.live.values()):
            if not handle.session.is_over():
                handle.session.submit_line("/end")
        for handle in list(self.live.values()):
            handle.session.wait(timeout=5)


def _workspace_path(session: Session, rel: str) -> str:
    if session.workspace is None:
        raise web.HTTPBadRequest(text="this session has no workspace")
    rel = str(rel).replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(session.workspace, rel))
    root = os.path.realpath(session.workspace)
    if not (full == root or full.startswith(root + os.sep)):
        raise web.HTTPBadRequest(text="path escapes the workspace")
    return full


def build_app(settings: Dict[str, Any]) -> Any:
    if web is None:
        raise WebUnavailable("the 'aiohttp' package is required for the web pane")

    manager = Manager(settings)
    ui_dir = settings["web"]["ui_dir"] or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui"
    )

    # Optional one-password login. When configured, every route except
    # /login requires the long-lived cookie; WS upgrades included
    # (browsers send cookies on the upgrade request).
    password_env = str(settings["web"].get("password_env", "") or "")
    password = os.environ.get(password_env, "") if password_env else ""
    password = password or str(settings["web"].get("password", ""))
    cookie_value = (
        hashlib.sha256(bytes("pane-auth:" + password, "utf-8")).hexdigest()
        if password
        else None
    )

    @web.middleware
    async def auth_middleware(request: Any, handler: Any) -> Any:
        if cookie_value is None or request.path == "/login":
            return await handler(request)
        given = request.cookies.get(_AUTH_COOKIE, "")
        if given and hmac.compare_digest(given, cookie_value):
            return await handler(request)
        if request.path.startswith("/api/"):
            raise web.HTTPUnauthorized(text="login required")
        raise web.HTTPFound("/login")

    app = web.Application(middlewares=[auth_middleware])

    async def login_get(request: Any) -> Any:
        if cookie_value is None:
            raise web.HTTPFound("/")
        return web.Response(text=_LOGIN_HTML % "", content_type="text/html")

    async def login_post(request: Any) -> Any:
        if cookie_value is None:
            raise web.HTTPFound("/")
        form = await request.post()
        given = str(form.get("password", ""))
        if not hmac.compare_digest(given, password):
            return web.Response(
                text=_LOGIN_HTML % "wrong password", content_type="text/html", status=401
            )
        response = web.HTTPFound("/")
        response.set_cookie(
            _AUTH_COOKIE,
            cookie_value,
            max_age=_AUTH_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=request.headers.get("X-Forwarded-Proto", "") == "https",
        )
        return response

    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)

    async def index(request: Any) -> Any:
        path = os.path.join(ui_dir, "index.html")
        if not os.path.isfile(path):
            raise web.HTTPNotFound(text="webui assets missing (looked in %s)" % ui_dir)
        with open(path, "r", encoding="utf-8") as fh:
            return web.Response(text=fh.read(), content_type="text/html")

    async def api_packs(request: Any) -> Any:
        return web.json_response(await asyncio.to_thread(manager.list_packs))

    async def api_sessions(request: Any) -> Any:
        return web.json_response(manager.list_sessions())

    async def api_create(request: Any) -> Any:
        body = await request.json()
        loop = asyncio.get_running_loop()
        try:
            handle = await asyncio.to_thread(
                manager.create,
                loop,
                body.get("pack", ""),
                minutes=float(body["minutes"]) if body.get("minutes") else None,
                task_id=body.get("task") or None,
                provider=body.get("provider") or None,
            )
        except (PackError, ValueError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(handle.meta())

    def _get_handle(request: Any) -> Handle:
        handle = manager.get(request.match_info["sid"])
        if handle is None:
            raise web.HTTPNotFound(text="no live session with that id")
        return handle

    async def api_meta(request: Any) -> Any:
        return web.json_response(_get_handle(request).meta())

    async def api_report(request: Any) -> Any:
        sid = os.path.basename(request.match_info["sid"])
        path = os.path.join(settings["paths"]["sessions_dir"], sid, "report.md")
        if not os.path.isfile(path):
            raise web.HTTPNotFound(text="no report yet")
        with open(path, "r", encoding="utf-8") as fh:
            return web.Response(text=fh.read(), content_type="text/plain", charset="utf-8")

    async def api_end(request: Any) -> Any:
        handle = _get_handle(request)
        if not handle.session.is_over():
            handle.session.submit_line("/end")
        return web.json_response({"ok": True})

    async def api_checks(request: Any) -> Any:
        handle = _get_handle(request)
        from . import checks as checks_mod

        rows = await asyncio.to_thread(events.read_transcript, handle.session.transcript.path)
        results = await asyncio.to_thread(
            checks_mod.run_for_session, rows, handle.session.pack, settings["run"]
        )
        return web.json_response(results)

    async def api_file_get(request: Any) -> Any:
        handle = _get_handle(request)
        full = _workspace_path(handle.session, request.rel_url.query.get("path", ""))
        if not os.path.isfile(full):
            return web.json_response({"content": "", "exists": False})
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return web.json_response({"content": fh.read(), "exists": True})

    async def api_file_put(request: Any) -> Any:
        handle = _get_handle(request)
        body = await request.json()
        full = _workspace_path(handle.session, body.get("path", ""))
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)

        def write() -> None:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(str(body.get("content", "")))

        await asyncio.to_thread(write)
        return web.json_response({"ok": True})

    async def ws_handler(request: Any) -> Any:
        handle = _get_handle(request)
        session = handle.session
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        handle.clients.add(q)
        try:
            resume = int(request.rel_url.query.get("resume", "0") or 0)
        except ValueError:
            resume = 0
        try:
            await ws.send_json({"hello": handle.meta()})
            snapshot = handle.rows
            if resume < snapshot:
                rows = await asyncio.to_thread(events.read_transcript, session.transcript.path)
                for i in range(resume, min(snapshot, len(rows))):
                    row = rows[i]
                    if row["kind"] in _STUB_KINDS:
                        row = {"t": row["t"], "ts": row.get("ts"), "kind": row["kind"], "stub": True}
                    await ws.send_json({"i": i, "row": row})

            async def pump() -> None:
                while True:
                    payload = await q.get()
                    await ws.send_json(payload)

            pump_task = asyncio.create_task(pump())
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if "say" in data:
                        text = str(data["say"]).strip()
                        if text and not text.startswith("/"):
                            session.submit_line(text)
                    elif "command" in data:
                        cmd = str(data["command"]).strip()
                        if cmd in ("/next", "/end", "/time", "/help"):
                            session.submit_line(cmd)
                    elif "run" in data:
                        cmd = str(data["run"]).strip()
                        if cmd:
                            await asyncio.to_thread(session.submit_line, "/run " + cmd)
                    elif "save" in data:
                        body = data["save"] or {}
                        try:
                            full = _workspace_path(session, body.get("path", ""))
                        except web.HTTPBadRequest as exc:
                            # a bad save must not tear down the stream
                            await ws.send_json({"notice": "save refused: %s" % exc.text})
                            continue
                        content = str(body.get("content", ""))

                        def write() -> None:
                            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
                            with open(full, "w", encoding="utf-8") as fh:
                                fh.write(content)

                        try:
                            await asyncio.to_thread(write)
                        except OSError as exc:
                            await ws.send_json({"notice": "save failed: %s" % exc})
                    elif "pulse" in data:
                        body = data["pulse"] or {}
                        session.submit_pulse(body.get("path", ""), body.get("delta", 0))
            finally:
                pump_task.cancel()
        finally:
            handle.clients.discard(q)
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/api/packs", api_packs)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_post("/api/sessions", api_create)
    app.router.add_get("/api/sessions/{sid}", api_meta)
    app.router.add_get("/api/sessions/{sid}/report", api_report)
    app.router.add_post("/api/sessions/{sid}/checks", api_checks)
    app.router.add_post("/api/sessions/{sid}/end", api_end)
    app.router.add_get("/api/sessions/{sid}/file", api_file_get)
    app.router.add_put("/api/sessions/{sid}/file", api_file_put)
    app.router.add_get("/api/sessions/{sid}/ws", ws_handler)
    if os.path.isdir(ui_dir):
        app.router.add_static("/ui", ui_dir)

    async def on_shutdown(app: Any) -> None:
        await asyncio.to_thread(manager.shutdown)

    app.on_shutdown.append(on_shutdown)
    return app


def serve(settings: Dict[str, Any], host: Optional[str] = None, port: Optional[int] = None) -> None:
    app = build_app(settings)
    web.run_app(
        app,
        host=host or settings["web"]["host"],
        port=int(port or settings["web"]["port"]),
        print=lambda *a, **k: print(
            "serving on http://%s:%s (bind stays private: localhost or tailnet only)"
            % (host or settings["web"]["host"], port or settings["web"]["port"])
        ),
    )
