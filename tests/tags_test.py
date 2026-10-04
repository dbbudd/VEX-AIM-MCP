"""AprilTags as field markers and obstacles, the field set-up, and saving it between runs. Runs its own
simulator (mock_robot.py --world goals) and panel, with a scratch setup file."""
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

HERE = Path(__file__).resolve().parent.parent
PY = sys.executable
MOCK_PORT, PANEL_PORT = 8871, 8751
BASE = f"http://127.0.0.1:{PANEL_PORT}"
SETUP = Path(tempfile.gettempdir()) / "aim_tags_test_setup.json"
failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def http(path, body=None, token=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Panel-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if path.startswith(("/state", "/api")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def state():
    return http("/state.json")[1]


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        s = state()
        if pred(s):
            return s
        time.sleep(0.2)
    return state()


def start_panel():
    env = dict(os.environ, AIM_PANEL_SETUP=str(SETUP))
    p = subprocess.Popen([PY, "-m", "vex_aim_mcp.panel", "--host", f"127.0.0.1:{MOCK_PORT}", "--port", str(PANEL_PORT), "--no-browser", "--no-yolo"],
                         cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            page = http("/")[1]
            return p, re.search(rb'const TOKEN = "(\w+)"', page).group(1).decode(), page
        except (urllib.error.URLError, ConnectionError, AttributeError):
            time.sleep(0.2)
    raise SystemExit("panel didn't start")


def tag(s, i):
    return next((d for d in s["detections"] if d["kind"] == "apriltag" and d["id"] == i), None)


SETUP.unlink(missing_ok=True)
mock = subprocess.Popen([PY, "-m", "vex_aim_mcp.mock_robot", "--port", str(MOCK_PORT), "--world", "goals"], cwd=HERE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
panel, token, page = start_panel()
try:
    check("page carries the field sizes and ruler", b'"soccer"' in page and b"__FIELDS__" not in page and b"__RULER__" not in page)
    s = wait_for(lambda s: s["robot"]["connected"])
    check("no tags until detection is on", s["robot"]["connected"] and tag(s, 3) is None)
    code, j = http("/api/apriltags", {"enabled": True}, token)
    s = wait_for(lambda s: tag(s, 3))
    t3 = tag(s, 3)
    check("tag 3 is detected once AprilTags are on", code == 200 and t3 is not None, t3 and (t3["bearing"], t3["size"]))
    check("a tag has no distance until measured", t3 and t3["distance_mm"] is None)
    s = wait_for(lambda s: "AprilTag" in s["camera_range"])  # the map notes sizes about four times a second
    check("camera range lists AprilTags once seen", "AprilTag" in s["camera_range"], s["camera_range"].get("AprilTag"))

    code, j = http("/api/calibrate", {"object": t3, "distance_cm": 50}, token)
    s = wait_for(lambda s: tag(s, 3) and tag(s, 3)["distance_mm"] == 500)
    check("measuring one tag gives tags a distance", code == 200 and tag(s, 3)["distance_mm"] == 500, j)
    s = wait_for(lambda s: [o for o in s["map"] if o["kind"] == "apriltag"])
    tags_on_map = [o for o in s["map"] if o["kind"] == "apriltag"]
    check("the tag goes on the map exactly once", len(tags_on_map) == 1 and tags_on_map[0]["id"] == 3, tags_on_map)

    code, j = http("/api/label", {"object": tag(s, 3), "label": "corner"}, token)
    code, j = http("/api/field", {"name": "soccer"}, token)
    s = state()
    check("the field is chosen on the panel", code == 200 and s["field"]["name"] == "soccer", s["field"])
    code, j = http("/api/field", {"name": "moon"}, token)
    check("an unknown field is refused", code == 400, j)

    code, j = http("/api/place_robot", {"x": 0, "y": -900}, token)
    s = state()
    off = s["field"]["offset"]
    check("placing the robot sets the offset", code == 200 and abs(off[0] - (0 - s["robot"]["x"])) < 1 and abs(off[1] - (-900 - s["robot"]["y"])) < 1
          and s["field"]["located_by"] == "placed by hand", s["field"])

    # pin tag 3 somewhere on the pitch: the robot should then work out where it is from it
    pin = [-300.0, -400.0]
    code, j = http("/api/tag", {"id": 3, "pin": pin}, token)
    check("pinning a tag makes it a marker", code == 200 and j["role"] == "marker" and j["pin"] == pin, j)
    time.sleep(3.5)
    s = state()
    d, R = tag(s, 3), s["robot"]
    a = math.radians(d["heading"])
    expect = [pin[0] - (R["x"] + d["distance_mm"] * math.sin(a)), pin[1] - (R["y"] + d["distance_mm"] * math.cos(a))]
    off = s["field"]["offset"]
    check("a pinned marker in view locates the robot", math.dist(off, expect) < 15 and s["field"]["located_by"].startswith("AprilTag 3"),
          (off, expect, s["field"]["located_by"]))
    check("finding itself is logged", any("worked out where it is on the field" in e["text"] for e in s["events"]) or
          any("worked out where it is" in e["text"] for e in http("/state.json?since=0")[1]["events"]))

    code, j = http("/api/tag", {"id": 5, "role": "obstacle"}, token)
    code2, j2 = http("/api/tag", {"id": 99, "role": "marker"}, token)
    code3, j3 = http("/api/tag", {"id": 5, "role": "lava"}, token)
    check("tag roles: obstacle accepted, bad id and role refused", code == 200 and j["role"] == "obstacle" and code2 == 400 and code3 == 400,
          (j, j2, j3))
    saved = json.loads(SETUP.read_text())
    check("the setup file holds the measurement, roles, pin, label and field",
          saved["field"] == "soccer" and saved["tags"]["3"]["role"] == "marker" and saved["tags"]["3"]["pin"] == pin and saved["tags"]["5"]["role"] == "obstacle"
          and "AprilTag" in saved["calibration"] and saved["tag_labels"][0]["label"] == "corner", saved)
finally:
    panel.terminate(); panel.wait(5)

# a restart: everything worth keeping comes back, and the robot finds itself again from the marker
panel, token, page = start_panel()
try:
    s = wait_for(lambda s: s["robot"]["connected"])
    check("after a restart: field, tags and label are back", s["field"]["name"] == "soccer" and s["tags"].get("3", {}).get("pin") == [-300.0, -400.0]
          and s["tags"].get("5", {}).get("role") == "obstacle" and any(e.get("tag") == 3 and e["label"] == "corner" for e in s["labels"]),
          (s["field"], s["tags"], s["labels"]))
    check("after a restart: the measurement is back", s["camera_range"].get("AprilTag", {}).get("measured") == 1, s["camera_range"].get("AprilTag"))
    http("/api/apriltags", {"enabled": True}, token)
    s = wait_for(lambda s: (s["field"]["located_by"] or "").startswith("AprilTag 3"), timeout=8)
    check("after a restart: the robot finds itself from the pinned marker", (s["field"]["located_by"] or "").startswith("AprilTag 3 “corner”"),
          s["field"])
finally:
    panel.terminate(); panel.wait(5)
    mock.terminate(); mock.wait(5)

# what Claude is told: obstacles relative to the robot, positions on the field
from vex_aim_mcp.panel import PanelState  # noqa: E402

st = PanelState(setup_file=None)
st.field.update(name="soccer", offset=[100.0, -900.0], located_by="placed by hand")
st.tags = {5: {"role": "obstacle", "pin": [100.0, -400.0]}, 3: {"role": "marker", "pin": [-600.0, 1200.0]}}
st.map = [{"key": ["ai_object", "SportsBall", 0], "kind": "ai_object", "name": "SportsBall", "id": 0, "x": 0.0, "y": 300.0,
           "seen": time.monotonic(), "display": "SportsBall", "sightings": 5}]
summ = st.summary((0.0, 0.0, 0.0))  # robot at odometry (0, 0) facing heading 0, so on the field at (100, -900)
ob = summ["obstacles"][0]
check("Claude: the robot's field position", summ["field"]["robot_mm"] == [100, -900], summ["field"])
check("Claude: obstacle straight ahead, 50 cm away", ob["distance_mm"] == 500 and ob["bearing_deg"] == 0 and ob["keep_out_mm"] == 150, ob)
check("Claude: map positions are on the field", summ["map"][0]["x_mm"] == 100 and summ["map"][0]["y_mm"] == -600 and "soccer" in summ["map_frame"],
      summ["map"])
check("Claude: tag roles and pins", {t["id"]: t["role"] for t in summ["apriltags"]} == {3: "marker", 5: "obstacle"}
      and summ["apriltags"][0]["pinned_mm"] == [-600, 1200], summ["apriltags"])

SETUP.unlink(missing_ok=True)
print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
