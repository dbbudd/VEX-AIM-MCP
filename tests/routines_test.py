"""Kick test, speed test, scanning, exploring, STOP and the player name, through the control panel's
API (as the person would use it), against the arena simulator. Runs its own simulator and panel."""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from websockets.sync.client import connect as ws_connect

HERE = Path(__file__).resolve().parent.parent
PY = sys.executable
MOCK_PORT, PANEL_PORT = 8872, 8752
BASE = f"http://127.0.0.1:{PANEL_PORT}"
TMP = Path(tempfile.gettempdir())
SETUP, MOCK_LOG = TMP / "aim_routines_test_setup.json", TMP / "aim_routines_test_mock.log"
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


def wait_routine_done(timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not state()["routine"]:
            return True
        time.sleep(0.3)
    return False


def log_since(seq):
    return [e["text"] for e in state(seq)["events"]]


def mock_cmd(cmd):
    with ws_connect(f"ws://127.0.0.1:{MOCK_PORT}/ws_cmd") as ws:
        ws.send(json.dumps(cmd).encode())
        return json.loads(ws.recv())


SETUP.unlink(missing_ok=True)
mock = subprocess.Popen([PY, "-m", "vex_aim_mcp.mock_robot", "--port", str(MOCK_PORT), "--world", "arena", "--ball-in-kicker"], cwd=HERE,
                        stdout=subprocess.DEVNULL, stderr=open(MOCK_LOG, "w"))
time.sleep(1)
panel = subprocess.Popen([PY, "-m", "vex_aim_mcp.panel", "--host", f"127.0.0.1:{MOCK_PORT}", "--port", str(PANEL_PORT), "--no-browser", "--no-yolo"],
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
    s = state()
    check("connected; the ball is in the kicker", s["robot"]["connected"] and s["robot"]["holding"] == "SportsBall", s["robot"].get("holding"))

    code, j = http("/api/kick_test", {"strength": "soft", "ball_confirmed": True})
    check("experiments need motion unlocked", code == 409 and "Unlock motion" in j.get("error", ""), (code, j))
    http("/api/motion", {"enabled": True})

    # soft kick: the simulator's ball stops about 40 cm out, well within the camera's range
    seq = state()["event_seq"]
    code, j = http("/api/kick_test", {"strength": "soft", "ball_confirmed": True})
    check("kick test starts", code == 200 and j.get("started") == "kick test", (code, j))
    code2, j2 = http("/api/scan", {})
    check("only one job at a time", code2 == 409 and "busy" in j2.get("error", ""), (code2, j2))
    wait_routine_done(15)
    s = state()
    lk = s["last_kick"]
    check("soft kick: the camera saw it stop about 42 cm out", lk and lk["stopped_mm"] and abs(lk["stopped_mm"] - 420) < 60, lk)
    check("soft kick: launch speed about 60 cm/s", lk and lk["launch_mm_s"] and abs(lk["launch_mm_s"] - 600) < 200, lk and lk["launch_mm_s"])
    check("soft kick saved as an ability", s["kick_reach"].get("soft", {}).get("tests") == 1, s["kick_reach"])
    check("the result is logged", any("soft kick went" in t for t in log_since(seq)), log_since(seq)[-3:])

    # medium kick: the ball rolls out of the camera's range, so the person measures it
    mock_cmd({"cmd_id": "mock_ball_in_kicker"})
    time.sleep(0.5)
    code, j = http("/api/kick_test", {"strength": "medium", "ball_confirmed": True})
    wait_routine_done(15)
    lk = state()["last_kick"]
    check("medium kick: rolled out of view, so no distance yet", lk and lk["strength"] == "medium" and lk["seen"] and not lk["stopped_mm"]
          and lk["last_seen_mm"] > 900, lk)
    code, j = http("/api/kick_record", {"strength": "medium", "distance_cm": 134})
    s = state()
    check("the measured medium kick is saved", code == 200 and s["kick_reach"]["medium"]["mm"] == 1340 and s["last_kick"]["stopped_mm"] == 1340,
          s["kick_reach"])
    code, j = http("/api/kick_record", {"strength": "hard", "distance_cm": 0})
    check("a silly distance is refused", code == 400, j)

    # speed test at 60% (120 mm/s asked): top speed and time to reach it
    pos0 = state()["robot"]
    code, j = http("/api/speed_test", {"speed_percent": 60, "distance_cm": 50})
    check("speed test starts", code == 200, j)
    wait_routine_done(20)
    s = state()
    d = s["abilities"]["drive"].get("60")
    check("speed test: top speed about 12 cm/s", d and abs(d["top_mm_s"] - 120) < 15, d)
    check("speed test: up to speed quickly, 50 cm in about 4.3 s", d and d["accel_s"] is not None and d["accel_s"] < 0.6
          and abs(d["time_s"] - 4.3) < 0.6, d)
    back = math.dist((s["robot"]["x"], s["robot"]["y"]), (pos0["x"], pos0["y"]))
    check("speed test: drove back to the start", back < 30, back)

    # STOP ends a job, whoever started it
    seq = state()["event_seq"]
    http("/api/scan", {})
    time.sleep(1.5)
    check("a scan is running", (state()["routine"] or {}).get("name") == "scan", state()["routine"])
    http("/api/stop", {})
    time.sleep(1.0)
    texts = log_since(seq)
    check("STOP ends it", state()["routine"] is None and any("STOP" in t for t in texts), texts[-3:])

    # explore the pitch: tags on, measure tag 5 and make it an obstacle, place the robot, go
    http("/api/apriltags", {"enabled": True})
    time.sleep(1.0)
    s = state()
    t5 = next((d for d in s["detections"] if d["kind"] == "apriltag" and d["id"] == 5), None)
    R = s["robot"]
    true_xy = (0.0 + R["x"], -700.0 + R["y"])  # the simulator's start is (0, -700) on the pitch
    if t5:
        http("/api/calibrate", {"object": t5, "distance_cm": math.dist(true_xy, (-250, 250)) / 10})
    http("/api/tag", {"id": 5, "role": "obstacle", "label": "cone", "note": "keep away from this"})
    http("/api/field", {"name": "soccer"})
    http("/api/place_robot", {"x": true_xy[0], "y": true_xy[1]})
    seq = state()["event_seq"]
    code, j = http("/api/explore", {"speed_percent": 60})
    check("exploring starts", code == 200 and j.get("started") == "exploration", (code, j))
    done = wait_routine_done(300)
    texts = log_since(seq)
    summary = next((t for t in texts if t.startswith("Explored")), None)
    check("exploring finishes", done and summary is not None, texts[-4:])
    print("     ", summary)
    m = state()["map"]
    names = sorted(o["name"] for o in m)
    check("the map found both goals", names.count("BlueBarrel") == 2 and names.count("OrangeBarrel") == 2, names)
    check("no bumps", not any("bump" in t for t in texts), [t for t in texts if "bump" in t])

    # the player's name on the robot's screen, in its team colour
    http("/api/team", {"colour": "blue"})
    code, j = http("/api/player", {"name": "Striker"})
    time.sleep(0.5)
    mock_log = MOCK_LOG.read_text()
    check("player name shown on the robot", code == 200 and j["player"] == "Striker" and '"Striker"' in mock_log
          and '"light_set"' in mock_log, j)

    saved = json.loads(SETUP.read_text())
    check("saved: abilities, player, tag meaning", saved["abilities"]["kick"].get("soft") and saved["abilities"]["kick"].get("medium")
          and saved["abilities"]["drive"].get("60") and saved["player"] == "Striker" and saved["tags"]["5"]["note"] == "keep away from this",
          {k: saved[k] for k in ("player", "tags")})
finally:
    panel.terminate(); panel.wait(5)
    mock.terminate(); mock.wait(5)
    SETUP.unlink(missing_ok=True)

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
