"""Driver control and matches, through the control panel's API, against the arena simulator.
Starts its own simulator (:8896) and panel (:8769)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MOCK_PORT, PANEL_PORT = 8874, 8754
BASE = f"http://127.0.0.1:{PANEL_PORT}"
SETUP = Path(tempfile.gettempdir()) / "aim_driver_test_setup.json"
failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def http(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    req.add_header("Content-Type", "application/json")
    if body is not None:
        req.add_header("X-Panel-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if path.startswith(("/state", "/api")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def state(since=0):
    return http(f"/state.json?since={since}")[1]


def hold(stick, seconds):
    """What the page does while a key is held: send the stick ten times a second."""
    end = time.time() + seconds
    while time.time() < end:
        http("/api/drive", stick)
        time.sleep(0.1)


SETUP.unlink(missing_ok=True)
mock = subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.mock_robot", "--port", str(MOCK_PORT), "--world", "arena"], cwd=HERE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
panel = subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.panel", "--host", f"127.0.0.1:{MOCK_PORT}", "--port", str(PANEL_PORT), "--no-browser", "--no-yolo"],
                         cwd=HERE, env=dict(os.environ, AIM_PANEL_SETUP=str(SETUP)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            TOKEN = re.search(rb'const TOKEN = "(\w+)"', http("/")[1]).group(1).decode()
            break
        except (urllib.error.URLError, ConnectionError, AttributeError):
            time.sleep(0.2)
    for _ in range(50):
        if state()["robot"]["connected"]:
            break
        time.sleep(0.2)
    check("starts in Auto", state()["mode"] == "auto")
    code, j = http("/api/drive", {"y": 1})
    check("driving needs Driver mode", code == 409 and "Driver mode" in j.get("error", ""), j)
    http("/api/mode", {"mode": "driver"})
    code, j = http("/api/drive", {"y": 1})
    check("driving needs motion unlocked", code == 409 and "Unlock motion" in j.get("error", ""), j)
    http("/api/motion", {"enabled": True})
    code, j = http("/api/scan", {})
    check("the robot's own jobs wait while you drive", code == 409 and "Driver mode" in j.get("error", ""), j)

    y0 = state()["robot"]["y"]
    hold({"y": 1}, 1.5)
    y1 = state()["robot"]["y"]
    check("holding forward drives forward (about 12 cm/s at the 60% cap)", 120 < y1 - y0 < 260, round(y1 - y0))
    time.sleep(0.6)
    a = state()["robot"]["y"]
    time.sleep(0.6)
    b = state()["robot"]["y"]
    check("letting go stops it", abs(b - a) < 3, (round(a), round(b)))

    h0 = state()["robot"]["heading"]
    hold({"r": 1}, 1.0)
    time.sleep(0.5)
    turned = (state()["robot"]["heading"] - h0) % 360
    check("holding turn-right turns it clockwise", 60 < turned < 160, round(turned))
    r0 = state()["robot"]
    hold({"x": 1}, 1.0)
    time.sleep(0.5)
    r1 = state()["robot"]
    moved = ((r1["x"] - r0["x"]) ** 2 + (r1["y"] - r0["y"]) ** 2) ** 0.5
    check("it strafes sideways without turning", moved > 60 and abs((r1["heading"] - r0["heading"] + 180) % 360 - 180) < 3,
          (round(moved), round(r1["heading"] - r0["heading"])))
    code, j = http("/api/driver_kick", {"strength": "soft"})
    check("the driver can kick", code == 200, j)

    # the robot's own AIM controller: its stick drives in Driver mode, with nothing from the page
    from websockets.sync.client import connect as ws_connect

    def controller(**values):
        with ws_connect(f"ws://127.0.0.1:{MOCK_PORT}/ws_cmd") as ws:
            ws.send(json.dumps({"cmd_id": "mock_controller", **values}).encode())
            ws.recv()

    check("no AIM controller to start with", state()["aim_controller"]["connected"] is False)
    r0 = state()["robot"]
    controller(stick_x=0, stick_y=100, battery=80)
    time.sleep(1.5)
    s = state()
    moved = ((s["robot"]["x"] - r0["x"]) ** 2 + (s["robot"]["y"] - r0["y"]) ** 2) ** 0.5
    check("pushing the AIM controller's stick drives it", s["aim_controller"]["connected"] and moved > 100, (round(moved), s["aim_controller"]))
    controller(stick_y=0)
    time.sleep(0.5)
    a = state()["robot"]
    time.sleep(0.6)
    b = state()["robot"]
    check("centring the stick stops it", abs(b["x"] - a["x"]) + abs(b["y"] - a["y"]) < 3)
    controller(battery=0)

    http("/api/mode", {"mode": "auto"})
    seq = state()["event_seq"]
    code, j = http("/api/match", {"action": "start", "auto_s": 1, "driver_s": 2})
    time.sleep(0.3)
    s = state()
    check("a match starts with the autonomous period", code == 200 and s["match"]["phase"] == "auto" and s["mode"] == "auto", s["match"])
    code, j = http("/api/mode", {"mode": "driver"})
    check("no switching modes by hand during a match", code == 409, j)
    time.sleep(1.2)
    s = state()
    check("then driver control", s["match"] and s["match"]["phase"] == "driver" and s["mode"] == "driver", s["match"])
    time.sleep(2.2)
    s = state(seq)
    texts = [e["text"] for e in s["events"]]
    check("then it's over: back to Auto", s["match"] is None and s["mode"] == "auto"
          and any("Match started" in t for t in texts) and any("Driver control" in t for t in texts) and "Match over" in texts, texts)

    # Linking an AIM controller: the panel lets go of the robot (its own menus only work with no program connected)
    code, j = http("/api/release", {})
    time.sleep(6)  # longer than the panel's 5-second reconnect tries
    s = state()
    check("Link a controller lets go of the robot, and the panel doesn't grab it back", code == 200 and s["released"]
          and not s["robot"]["connected"], (j, s["released"], s["robot"].get("connected")))
    code, j = http("/api/resume", {})
    s = state()
    check("Done reconnects", code == 200 and not s["released"] and s["robot"]["connected"], (j, s["released"]))
finally:
    panel.terminate(); panel.wait(5)
    mock.terminate(); mock.wait(5)
    SETUP.unlink(missing_ok=True)

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
