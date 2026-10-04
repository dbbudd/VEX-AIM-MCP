"""The MCP server over HTTP (vex-aim-mcp --http), the way ChatGPT reaches it through a tunnel. Starts a
simulator on :8866 and the server on :8757 (its control panel on :8758), and connects as "openai-mcp"."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation

PY = sys.executable
MOCK_PORT, HTTP_PORT, PANEL_PORT, SECRET = 8866, 8757, 8758, "test-secret-123"
URL = f"http://127.0.0.1:{HTTP_PORT}/{SECRET}/mcp"
SETUP = Path(tempfile.gettempdir()) / "aim_http_test_setup.json"
failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def listening(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def post(url, host=None):
    """A bare MCP initialize request, as a remote app would send it."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def session_checks():
    async with streamable_http_client(URL) as (read, write, *_):
        async with ClientSession(read, write, client_info=Implementation(name="openai-mcp", version="1.0")) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            check("over HTTP, every tool is listed", len(tools) >= 40 and {"look", "move", "control_panel"} <= tools, f"{len(tools)} tools")
            r = await s.call_tool("robot_status", {})
            text = " ".join(getattr(c, "text", "") for c in r.content)
            check("a tool works over HTTP", not r.is_error and "attery" in text, text[:120])
            r = await s.call_tool("control_panel", {"enabled": True})
            text = " ".join(getattr(c, "text", "") for c in r.content)
            check("the control panel opens, and the reply gives the link", not r.is_error and f"127.0.0.1:{PANEL_PORT}" in text, text[:160])
            with urllib.request.urlopen(f"http://127.0.0.1:{PANEL_PORT}/state.json", timeout=5) as resp:
                state = json.load(resp)
            check("the panel calls the assistant ChatGPT", state.get("assistant") == "ChatGPT", state.get("assistant"))
            check("the panel's log names the assistant's tool calls", any(e["who"] == "assistant" for e in state["events"]),
                  sorted({e["who"] for e in state["events"]}))


SETUP.unlink(missing_ok=True)
mock = subprocess.Popen([PY, "-m", "vex_aim_mcp.mock_robot", "--port", str(MOCK_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
server = subprocess.Popen([PY, "-m", "vex_aim_mcp", "--http", "--port", str(HTTP_PORT), "--secret", SECRET],
                          env={**os.environ, "AIM_HOST": f"127.0.0.1:{MOCK_PORT}", "AIM_LIVE_VIEW_PORT": str(PANEL_PORT),
                               "AIM_PANEL_SETUP": str(SETUP)},
                          stdout=subprocess.DEVNULL, stderr=open(Path(tempfile.gettempdir()) / "aim_http_server.log", "w"))
try:
    for _ in range(100):
        if listening(HTTP_PORT):
            break
        time.sleep(0.2)
    check("the server listens on this computer only", listening(HTTP_PORT))
    check("the secret address answers", post(URL) == 200)
    check("the plain /mcp address doesn't", post(f"http://127.0.0.1:{HTTP_PORT}/mcp") == 404)
    check("a request through a tunnel (another host name) is accepted", post(URL, host="example.trycloudflare.com") == 200)
    asyncio.run(session_checks())
finally:
    server.terminate(); server.wait(5)
    mock.terminate(); mock.wait(5)
    SETUP.unlink(missing_ok=True)

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
