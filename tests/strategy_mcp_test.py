"""Claude's new tools over MCP, against the arena simulator with the ball in the kicker:
test_kick, record_kick_distance, test_speed, explore_arena, react, and stop ending a job."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent.parent
PY = sys.executable
MOCK_PORT, PANEL_PORT = 8873, 8753
SETUP = Path(tempfile.gettempdir()) / "aim_strategy_mcp_setup.json"
failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def text(r):
    return "\n".join(c.text for c in r.content if getattr(c, "type", "") == "text")


def is_error(r):
    return bool(getattr(r, "is_error", None) or getattr(r, "isError", None))


async def call(s, name, args=None, expect_error=False):
    t = time.monotonic()
    r = await s.call_tool(name, args or {})
    body = text(r).replace("\n", " | ")
    check(f"{name}({json.dumps(args or {})})" + (" -> refused as expected" if expect_error else ""),
          is_error(r) == expect_error, f"{time.monotonic() - t:.1f}s: {body[:260]}")
    return body


async def main():
    env = {**os.environ, "AIM_HOST": f"127.0.0.1:{MOCK_PORT}", "AIM_LIVE_VIEW_PORT": str(PANEL_PORT), "AIM_PANEL_SETUP": str(SETUP)}
    params = StdioServerParameters(command=PY, args=["-m", "vex_aim_mcp"], env=env)
    async with stdio_client(params, errlog=open(Path(tempfile.gettempdir()) / "aim_strategy_server.log", "w")) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            check("the new tools are listed", {"explore_arena", "test_kick", "record_kick_distance", "test_speed", "react"} <= names,
                  f"{len(names)} tools")
            await call(s, "test_kick", {"strength": "soft"}, expect_error=True)  # motion is locked
            await call(s, "enable_motion", {"confirmed_clear_floor": True})
            body = await call(s, "test_kick", {"strength": "soft"})
            check("soft kick measured by the camera", "stopped about" in body, body[:120])
            body = await call(s, "record_kick_distance", {"strength": "medium", "distance_cm": 130})
            check("a measured kick is saved", "average 130 cm" in body, body)
            body = await call(s, "test_speed", {"speed_percent": 60, "distance_mm": 400})
            check("speed test reports a top speed", "top speed" in body and "drove back" in body, body[:140])
            status = await call(s, "robot_status")
            check("robot_status shows the abilities", '"kick_reach"' in status and '"drive_speed_mm_s"' in status)
            body = await call(s, "react", {"expression": "excited"})
            check("react shows a face", "excited" in body, body)
            await call(s, "react", {"expression": "angry"}, expect_error=True)

            async def stop_soon():
                await asyncio.sleep(3)
                return await s.call_tool("stop", {})

            stopper = asyncio.create_task(stop_soon())
            r = await s.call_tool("explore_arena", {"speed_percent": 60})
            stop_r = await stopper
            check("stop ends an exploration in progress", is_error(r) and "stopped (STOP)" in text(r)
                  and "ended what the robot was doing" in text(stop_r), (text(r)[:120], text(stop_r)[:80]))
            body = await call(s, "explore_arena", {"speed_percent": 60})
            check("without a field, exploring scans where it is", "Scanned a full circle" in body, body[:140])

            # while the person drives (Driver mode in the panel), Claude's movement tools refuse; stop still works
            import re
            import urllib.request
            page = urllib.request.urlopen(f"http://127.0.0.1:{PANEL_PORT}/", timeout=5).read()
            token = re.search(rb'const TOKEN = "(\w+)"', page).group(1).decode()

            def panel_api(action, body):
                req = urllib.request.Request(f"http://127.0.0.1:{PANEL_PORT}/api/{action}", data=json.dumps(body).encode(),
                                             method="POST", headers={"Content-Type": "application/json", "X-Panel-Token": token})
                return json.loads(urllib.request.urlopen(req, timeout=5).read())

            panel_api("mode", {"mode": "driver"})
            for name, args in [("move", {"distance_mm": 50}), ("turn", {"degrees": 30}), ("turn_to_heading", {"heading_deg": 90}),
                               ("kick", {"strength": "soft"}), ("test_speed", {"distance_mm": 200})]:
                body = await call(s, name, args, expect_error=True)
                check(f"{name} refuses while the person drives", "Driver mode" in body, body[:100])
            await call(s, "stop")
            panel_api("mode", {"mode": "auto"})
            await call(s, "move", {"distance_mm": 50})


SETUP.unlink(missing_ok=True)
mock = subprocess.Popen([PY, "-m", "vex_aim_mcp.mock_robot", "--port", str(MOCK_PORT), "--world", "arena", "--ball-in-kicker"], cwd=HERE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
try:
    asyncio.run(main())
finally:
    mock.terminate(); mock.wait(5)
    SETUP.unlink(missing_ok=True)
print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
