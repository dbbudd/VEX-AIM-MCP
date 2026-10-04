"""The team list in the control panel, against mock_arena.py's two robots on one pitch.
Starts the robots on :8877 and :8878 and a panel on :8756."""
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
ROBOT1, ROBOT2, PANEL_PORT = "127.0.0.1:8877", "127.0.0.1:8878", 8756
BASE = f"http://127.0.0.1:{PANEL_PORT}"
SETUP = Path(tempfile.gettempdir()) / "aim_team_panel_test_setup.json"
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
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if path.startswith(("/state", "/api")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def state():
    return http("/state.json")[1]


def start_panel():
    global TOKEN
    p = subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.panel", "--host", ROBOT1, "--port", str(PANEL_PORT), "--no-browser", "--no-yolo"], cwd=HERE,
                         env=dict(os.environ, AIM_PANEL_SETUP=str(SETUP)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            TOKEN = re.search(rb'const TOKEN = "(\w+)"', http("/")[1]).group(1).decode()
            if state()["robot"]["connected"]:
                break
        except (urllib.error.URLError, ConnectionError, AttributeError):
            pass
        time.sleep(0.25)
    return p


SETUP.unlink(missing_ok=True)
arena = subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.mock_arena", "--port", "8877", "--robots", "2"], cwd=HERE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
panel = start_panel()
try:
    s = state()
    check("the panel's robot is on the team list by itself, selected", len(s["players"]) == 1 and s["players"][0]["selected"]
          and s["players"][0]["host"] == ROBOT1 and s["players"][0]["connected"], s["players"])
    code, j = http("/api/player_add", {"name": "Rocket", "host": ROBOT2, "team": "orange"})
    s = state()
    rocket = next((p for p in s["players"] if p["name"] == "Rocket"), {})
    check("adding a robot by its address connects it", code == 200 and len(s["players"]) == 2 and rocket.get("connected"), rocket)
    code, j = http("/api/player_add", {"name": "rocket", "host": "127.0.0.1:9999"})
    check("names are unique", code == 409, j)
    code, j = http("/api/players_label", {})
    check("Label all puts every player's card on its robot", code == 200 and all(e is None for e in j["results"].values()), j)
    code, j = http("/api/player_select", {"name": "Rocket"})
    time.sleep(1)
    s = state()
    check("Show switches the panel to that robot", code == 200 and s["robot"]["host"] == ROBOT2 and s["player"] == "Rocket"
          and s["team"] == "orange", (s["robot"].get("host"), s["player"], s["team"]))
    code, j = http("/api/player_remove", {"name": "Rocket"})
    check("the robot the panel shows can't be removed", code == 409, j)
    first = next(p["name"] for p in s["players"] if p["host"] == ROBOT1)
    code, j = http("/api/team", {"colour": "blue"})
    check("the team cards change the shown player's team", next(p for p in state()["players"] if p["name"] == "Rocket")["team"] == "blue")
    saved = json.loads(SETUP.read_text())
    check("the team list is saved", sorted(p["name"] for p in saved["fleet"]) == sorted([first, "Rocket"]), saved.get("fleet"))
finally:
    panel.terminate(); panel.wait(5)

panel = start_panel()  # a restart: the list comes back, and the panel's own robot is matched to its player
try:
    s = state()
    check("after a restart the team list is back", sorted(p["name"] for p in s["players"]) == sorted([first, "Rocket"])
          and next(p for p in s["players"] if p["selected"])["host"] == ROBOT1, s["players"])
    code, j = http("/api/player_remove", {"name": "Rocket"})
    check("another robot can be taken off the list", code == 200 and len(j["players"]) == 1, j)
finally:
    panel.terminate(); panel.wait(5)
    arena.terminate(); arena.wait(5)
    SETUP.unlink(missing_ok=True)

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
