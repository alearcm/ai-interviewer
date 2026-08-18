"""The web pane, end to end in process: real manager, real sessions,
real watcher, canned adapter, aiohttp test server. Also proves the
transcript-as-wire-format resume: a reconnect replays exactly the rows
already recorded."""

import asyncio
import json
import os

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from harness.web import build_app  # noqa: E402

MINI_WORKSPACE_PACK = """
[pack]
name = "webw"
title = "Web workspace mini"

[session]
minutes = 0.5
workspace = true
primary_file = "answer.txt"
idle_threshold_s = 30
debounce_ms = 150

[interviewer]
persona = "Terse test interviewer."
opening_line = "Begin."
fallback_lines = ["Okay.", "Go on."]

[hints]
ladder = []

[tasks]
dir = "tasks"

[[rules]]
id = "reply"
on = ["user_message"]
when = "true"
action = "speak"
hint = "none"
priority = 10

[report]
title = "Mini web report"
[[report.sections]]
type = "messages"
"""

MINI_TASK = """
[task]
id = "wt1"
title = "Web task"
statement = "Type things."
"""

MINI_VERBAL_PACK = MINI_WORKSPACE_PACK.replace('name = "webw"', 'name = "webv"').replace(
    "workspace = true", "workspace = false"
).replace('primary_file = "answer.txt"\n', "")


def make_env(tmp_path):
    packs = tmp_path / "packs"
    for name, text in (("webw", MINI_WORKSPACE_PACK), ("webv", MINI_VERBAL_PACK)):
        d = packs / name
        (d / "tasks").mkdir(parents=True)
        (d / "pack.toml").write_text(text, encoding="utf-8")
        (d / "tasks" / "t.toml").write_text(MINI_TASK, encoding="utf-8")
    return {
        "model": {"provider": "canned"},
        "run": {"timeout_s": 10.0, "output_max_chars": 2000},
        "paths": {"sessions_dir": str(tmp_path / "sessions")},
        "web": {"host": "127.0.0.1", "port": 0, "ui_dir": "", "packs_dir": str(packs)},
    }


async def recv_rows(ws, until, timeout=20.0, bag=None, stage="?"):
    """Collect streamed messages until `until(bag)` is true."""
    bag = bag if bag is not None else {"rows": [], "hello": None, "notices": []}
    end = asyncio.get_event_loop().time() + timeout
    while not until(bag):
        left = end - asyncio.get_event_loop().time()
        try:
            assert left > 0
            msg = await asyncio.wait_for(ws.receive(), timeout=left)
        except (AssertionError, asyncio.TimeoutError):
            raise AssertionError(
                "stage %r timed out; got kinds=%r notices=%r"
                % (stage, [r["row"]["kind"] for r in bag["rows"]], bag["notices"])
            ) from None
        assert msg.type == aiohttp.WSMsgType.TEXT, msg
        data = json.loads(msg.data)
        if "hello" in data:
            bag["hello"] = data["hello"]
        elif "notice" in data:
            bag["notices"].append(data["notice"])
        elif "i" in data:
            # dedupe across the replay/live seam by row index, exactly
            # as the real client does
            if not any(r["i"] == data["i"] for r in bag["rows"]):
                bag["rows"].append(data)
    return bag


def kinds(bag):
    return [r["row"]["kind"] for r in bag["rows"]]


async def scenario(tmp_path):
    settings = make_env(tmp_path)
    app = build_app(settings)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # packs are discovered
        packs = await (await client.get("/api/packs")).json()
        assert {p["name"] for p in packs} == {"webv", "webw"}

        # create a workspace session
        res = await client.post("/api/sessions", json={"pack": "webw"})
        assert res.status == 200, await res.text()
        meta = await res.json()
        sid = meta["id"]
        assert meta["has_workspace"] and meta["primary_file"] == "answer.txt"

        ws = await client.ws_connect(f"/api/sessions/{sid}/ws?resume=0")
        bag = await recv_rows(ws, lambda b: b["hello"] is not None and "task_presented" in kinds(b), stage="hello")

        # chat -> user_message + canned reply
        await ws.send_json({"say": "hello there"})
        await recv_rows(ws, lambda b: "interviewer_message" in kinds(b)[-3:] and any(
            r["row"].get("text") == "hello there" for r in b["rows"]), bag=bag, stage="say")

        # pulse -> edit_pulse row
        await ws.send_json({"pulse": {"path": "answer.txt", "delta": 5}})
        await recv_rows(ws, lambda b: "edit_pulse" in kinds(b), bag=bag, stage="pulse")

        # save -> flows through the real watcher into file_saved
        await ws.send_json({"save": {"path": "answer.txt", "content": "final answer\n"}})
        await recv_rows(ws, lambda b: any(
            r["row"]["kind"] == "file_saved" and "final answer" in r["row"]["content"]
            for r in b["rows"]), bag=bag, stage="save")

        # run -> run_executed with captured output
        await ws.send_json({"run": "printf observed"})
        await recv_rows(ws, lambda b: any(
            r["row"]["kind"] == "run_executed" and "observed" in r["row"].get("out", "")
            for r in b["rows"]), bag=bag, stage="run")

        # end -> session_end row, then the report exists
        await ws.send_json({"command": "/end"})
        await recv_rows(ws, lambda b: kinds(b) and kinds(b)[-1] == "session_end", bag=bag, stage="end")
        await ws.close()

        report = await client.get(f"/api/sessions/{sid}/report")
        assert report.status == 200
        assert "hello there" in await report.text()

        # resume replays the exact same row sequence
        ws2 = await client.ws_connect(f"/api/sessions/{sid}/ws?resume=0")
        bag2 = await recv_rows(
            ws2, lambda b: len(b["rows"]) >= len(bag["rows"]), stage="resume"
        )
        assert kinds(bag2)[: len(kinds(bag))] == kinds(bag)
        first = bag2["rows"][0]["row"]
        assert first["kind"] == "session_start"
        assert bag2["rows"][1]["row"].get("stub") is True  # pack_snapshot stubbed
        await ws2.close()

        # a no-workspace pack serves chat-only sessions from the same app
        res = await client.post("/api/sessions", json={"pack": "webv"})
        meta_v = await res.json()
        assert meta_v["has_workspace"] is False
        ws3 = await client.ws_connect(f"/api/sessions/{meta_v['id']}/ws?resume=0")
        await ws3.send_json({"command": "/end"})
        await recv_rows(ws3, lambda b: "session_end" in kinds(b), stage="verbal-end")
        await ws3.close()

        # path traversal is refused
        bad = await client.get(f"/api/sessions/{sid}/file?path=../outside.txt")
        assert bad.status == 400
    finally:
        await client.close()


def test_web_pane_end_to_end(tmp_path):
    asyncio.run(scenario(tmp_path))
