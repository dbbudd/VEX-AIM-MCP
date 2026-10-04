"""Control panel + real-time tools, end to end against the SIMULATOR (mock_robot.py --world goals).
Acts as Claude (MCP over stdio) and as the person (HTTP calls the panel page makes).
Starts its own simulator on 127.0.0.1:8870 and the panel on 8750."""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from vex_aim_mcp.aim_client import vision_x_for_bearing  # noqa: E402

BASE = "http://127.0.0.1:8750"
OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="aim_panel_test_")
failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def text(r):
    return "\n".join(c.text for c in r.content if getattr(c, "type", "") == "text")


def is_error(r):
    return bool(getattr(r, "is_error", None) or getattr(r, "isError", None))


def http(path, body=None, token=None, origin=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Panel-Token", token)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def get(path):
    return await asyncio.to_thread(http, path)


async def post(path, body, token, origin=None):
    return await asyncio.to_thread(http, path, body, token, origin)


async def call(s, name, args=None, expect_error=False):
    t = time.monotonic()
    r = await s.call_tool(name, args or {})
    body = text(r).replace("\n", " | ")
    check(f"{name}({json.dumps(args or {})})" + (" -> refused as expected" if expect_error else ""),
          is_error(r) == expect_error, f"{time.monotonic() - t:.2f}s: {body[:230]}")
    return body


async def main():
    env = {**os.environ, "AIM_HOST": "127.0.0.1:8870", "AIM_LIVE_VIEW_PORT": "8750",
           "AIM_PANEL_SETUP": os.path.join(OUT, "panel_setup.json")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "vex_aim_mcp"], env=env)
    with open(f"{OUT}/panel_server.log", "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                check("new tools listed", {"control_panel", "wait_for", "teach_colour"} <= set(names), f"{len(names)} tools")
                await call(s, "control_panel")

                status, page = await get("/")
                token = re.search(rb'const TOKEN = "(\w+)"', page).group(1).decode()
                check("panel page served with a token", status == 200 and len(token) == 32)
                for _ in range(20):  # the panel connects to the robot on its own
                    state = json.loads((await get("/state.json"))[1])
                    if state["robot"]["connected"] and state["detections"]:
                        break
                    await asyncio.sleep(0.25)
                dets = state["detections"]
                check("state.json: robot connected, sees both blue posts", state["robot"]["connected"] and
                      sum(d["name"] == "BlueBarrel" for d in dets) == 2, [(d["display"], d["bearing"]) for d in dets])

                code, _ = await post("/api/team", {"colour": "orange"}, token=None)
                check("command without the page's token is refused", code == 403, code)
                code, _ = await post("/api/team", {"colour": "orange"}, token, origin="https://evil.example")
                check("command from another website is refused", code == 403, code)
                code, _ = await post("/api/team", {"colour": "orange"}, token)
                check("person picks orange team in the panel", code == 200, code)
                status_text = await call(s, "robot_status")
                check("Claude sees the team via robot_status().panel", '"team": "orange"' in status_text.replace("| ", ""))

                left = min((d for d in dets if d["name"] == "BlueBarrel"), key=lambda d: d["bearing"])

                async def person_points_out():
                    await asyncio.sleep(1.5)
                    await post("/api/select", {"object": left, "note": "fetch this one"}, token)

                pointing = asyncio.create_task(person_points_out())
                body = await call(s, "wait_for", {"event": "panel", "timeout_s": 10})
                await pointing
                check("wait_for('panel') wakes up when the person points something out",
                      "pointed out BlueBarrel" in body and "fetch this one" in body)

                code, _ = await post("/api/label", {"object": left, "label": "left post"}, token)
                check("person labels the left blue barrel", code == 200, code)
                body = await call(s, "detect_objects")
                check("label shows up in Claude's detections", '"label": "left post"' in body.replace("| ", ""))
                body = await call(s, "look")
                check("look() names it too", "“left post”" in body)

                cx = vision_x_for_bearing(22) * 2  # the simulator's red cup, in 640x480 photo pixels
                body = await call(s, "teach_colour", {"label": "red cup", "box_xyxy": [cx - 10, 226, cx + 10, 266]})
                check("teach_colour: the robot detects the cup by itself", "can see it now" in body)
                await call(s, "wait_for", {"event": "seen", "label": "red cup", "timeout_s": 5})
                await call(s, "approach_object", {"label": "nothing called this"}, expect_error=True)
                await call(s, "enable_motion", {"confirmed_clear_floor": True})
                body = await call(s, "face_object", {"label": "red cup"})
                check("face_object by taught colour", "Facing red cup" in body)
                t = time.monotonic()
                body = await call(s, "wait_for", {"event": "gone", "label": "red cup", "timeout_s": 3})
                check("wait_for times out cleanly", "still in view" in body and 2.8 < time.monotonic() - t < 4.5)
                body = await call(s, "wait_for", {"event": "seen", "target": "sports_ball", "timeout_s": 2})
                check("wait_for('seen') timeout message", "without seeing" in body)

                code, _ = await post("/api/stop", {}, token)
                check("STOP from the panel", code == 200, code)
                state = json.loads((await get("/state.json?since=0"))[1])
                whos = {e["who"] for e in state["events"]}
                check("log has entries from you, the assistant and the robot", {"you", "assistant", "robot"} <= whos, whos)
                check("taught colour listed in panel state", any(c["label"] == "red cup" for c in state["colours"].values()))
                for e in list(state["events"])[-8:]:
                    print(f"     log: [{e['who']}] {e['text'][:110]}")
                await call(s, "control_panel", {"enabled": False})
    print("\nRESULT:", "all passed" if not failures else f"{len(failures)} failed: {failures}")


mock = subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.mock_robot", "--port", "8870", "--world", "goals"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
try:
    asyncio.run(main())
finally:
    mock.terminate()
    mock.wait(5)
