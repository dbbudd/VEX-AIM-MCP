"""The network utility and pairing, through the control panel's API, against simulators with setup pages.
Starts simulators on :8875 and :8876 (and later :8879, the second robot at a new address, and :8868,
standing in for a robot's own hotspot) and a panel on :8755, which only scans those. Venue passwords go
in a test keychain entry, removed at the end."""
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
PANEL_PORT, ROBOTS = 8755, ["127.0.0.1:8875", "127.0.0.1:8876"]
MOVED, HOTSPOT, EMPTY = "127.0.0.1:8879", "127.0.0.1:8868", "127.0.0.1:8877"
BASE = f"http://127.0.0.1:{PANEL_PORT}"
SETUP = Path(tempfile.gettempdir()) / "aim_network_test_setup.json"
KEYCHAIN = "VEX AIM venue Wi-Fi (test)"
# The second robot's radio is at :8876, then (after switching networks) at :8879. The first robot's hotspot
# address is one more than its Wi-Fi address, as on the real radios.
MACS = {ROBOTS[0]: "f4:12:fa:00:00:20", ROBOTS[1]: "f4:12:fa:00:00:10", MOVED: "f4:12:fa:00:00:10", HOTSPOT: "f4:12:fa:00:00:21"}
# What the scan helper would report: one network on two bands and two access points, one 5 GHz only, one name too long
SCAN = {"permission": "allowed", "current": "Classroom", "networks": [
    {"ssid": "Classroom", "rssi": -65, "channel": 6, "band": "2.4 GHz", "open": False},
    {"ssid": "Classroom", "rssi": -45, "channel": 36, "band": "5 GHz", "open": False},
    {"ssid": "Classroom", "rssi": -50, "channel": 1, "band": "2.4 GHz", "open": False},
    {"ssid": "Competition Hall", "rssi": -70, "channel": 11, "band": "2.4 GHz", "open": False},
    {"ssid": "Fast5G", "rssi": -40, "channel": 149, "band": "5 GHz", "open": False},
    {"ssid": "A network name too long for it", "rssi": -80, "channel": 3, "band": "2.4 GHz", "open": True}]}
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
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if path.startswith(("/state", "/api")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def state():
    return http("/state.json")[1]


def page(host):
    with urllib.request.urlopen(f"http://{host}/", timeout=5) as r:
        return r.read().decode()


def keychain(network):
    r = subprocess.run(["security", "find-generic-password", "-a", network, "-s", KEYCHAIN, "-w"], capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def simulator(host):
    return subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.mock_robot", "--port", host.split(":")[1]], cwd=HERE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def answers(host):
    try:
        return bool(page(host))
    except OSError:
        return False


def wait_for(test, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if test():
            return True
        time.sleep(0.5)
    return False


def player(name):
    return next((p for p in state()["players"] if p["name"] == name), {})


SETUP.unlink(missing_ok=True)
for leftover in ("Classroom", "Competition Hall", "Library Wi-Fi"):
    subprocess.run(["security", "delete-generic-password", "-a", leftover, "-s", KEYCHAIN], capture_output=True)
mocks = {h: simulator(h) for h in ROBOTS}
time.sleep(1)
panel = subprocess.Popen([sys.executable, "-m", "vex_aim_mcp.panel", "--host", ROBOTS[0], "--port", str(PANEL_PORT), "--no-browser", "--no-yolo"], cwd=HERE,
                         env=dict(os.environ, AIM_PANEL_SETUP=str(SETUP), AIM_SCAN_HOSTS=",".join(ROBOTS + [EMPTY, MOVED]),
                                  AIM_KEYCHAIN_SERVICE=KEYCHAIN, AIM_KNOWN_NETWORKS="Classroom,Competition Hall,A network name too long for it",
                                  AIM_HOTSPOT_HOST=HOTSPOT, AIM_TEST_MACS=",".join(f"{h}={m}" for h, m in MACS.items()),
                                  AIM_TEST_SCAN=json.dumps(SCAN), AIM_TEST_MAC_PASSWORDS="Library Wi-Fi=readinglots"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            TOKEN = re.search(rb'const TOKEN = "(\w+)"', http("/")[1]).group(1).decode()
            break
        except (urllib.error.URLError, ConnectionError, AttributeError):
            time.sleep(0.2)
    wait_for(lambda: state()["robot"]["connected"], 10)
    me = state()["player"]

    # ---- what's where
    code, j = http("/api/net_info", {})
    check("the robot's network comes from its setup page", code == 200 and j["robot"]["network"] == "Classroom"
          and j["robot"]["firmware"] == "1.0.3.0" and j["host"] == ROBOTS[0], j)
    check("the robot's password is never passed on", "not-a-real-one" not in json.dumps(j))
    check("the networks this Mac has joined are offered", j["known"] == ["Classroom", "Competition Hall", "A network name too long for it"], j["known"])
    code, j = http("/api/net_find", {})
    found = [r["host"] for r in j.get("robots", [])]
    check("Find robots finds both simulators, and not the empty port", found == ROBOTS, found)
    check("…marks the one the panel uses, and gives hardware addresses", [r["this"] for r in j["robots"]] == [True, False]
          and j["robots"][1]["mac"] == MACS[ROBOTS[1]], j["robots"])

    # ---- pairing
    code, j = http("/api/player_pair", {"host": ROBOTS[1], "name": "Rocket", "team": "orange"})
    rocket = player("Rocket")
    check("Pair puts a found robot on the team, connected", code == 200 and not j.get("warning") and rocket.get("connected")
          and rocket.get("team") == "orange", (j, rocket))
    code, j = http("/api/player_pair", {"host": MOVED, "name": "rocket"})
    check("pairing under a name that's taken is refused", code == 409, j)
    code, j = http("/api/net_blink", {"host": ROBOTS[1]})
    check("Blink flashes a robot", code == 200, j)
    saved = json.loads(SETUP.read_text())["fleet"]
    check("the team list keeps each robot's hardware address", {p["name"]: p.get("mac") for p in saved}
          == {me: MACS[ROBOTS[0]], "Rocket": MACS[ROBOTS[1]]}, saved)

    # ---- remembering networks
    code, j = http("/api/net_remember", {})
    check("Remember copies the robot's own network into the keychain", code == 200 and j["venues"] == ["Classroom"]
          and keychain("Classroom") == "not-a-real-one", j)
    code, j = http("/api/net_remember", {"network": "Competition Hall", "password": "goteam2026"})
    check("a network picked from the list is remembered", code == 200 and j["venues"] == ["Classroom", "Competition Hall"]
          and keychain("Competition Hall") == "goteam2026", j)
    code, j = http("/api/net_scan", {})
    check("Scan lists nearby networks once each, the ones robots can join first, strongest first", code == 200
          and [n["ssid"] for n in j["networks"]] == ["Classroom", "Competition Hall", "Fast5G", "A network name too long for it"]
          and j["networks"][0]["bands"] == ["2.4 GHz", "5 GHz"] and j["networks"][0]["rssi"] == -45, j)
    check("…and says which a robot can't join (5 GHz only, or a name too long)", [n["robot_ok"] for n in j["networks"]] == [True, True, False, False], j)
    code, j = http("/api/net_remember", {"network": "Library Wi-Fi", "from_mac": True})
    check("Use this Mac's password remembers a network with the password the Mac saved", code == 200
          and "Library Wi-Fi" in j["venues"] and keychain("Library Wi-Fi") == "readinglots", j)
    code, j = http("/api/net_remember", {"network": "Nowhere", "from_mac": True})
    check("…and says so when the Mac has none", code == 409 and "type it in" in j.get("error", ""), j)
    code, j = http("/api/net_move", {"network": "A" * 21, "password": "x" * 9})
    check("a name too long for the robot is refused", code == 400 and "20 characters" in j.get("error", ""), j)
    code, j = http("/api/net_move", {"network": "Hall", "password": "short"})
    check("a too-short password is refused", code == 400, j)
    code, j = http("/api/net_move", {"network": "Classroom", "host": "127.0.0.1:8867"})
    check("a robot that isn't there isn't reported as switched", code == 409 and "Couldn't reach" in j.get("error", ""), j)

    # ---- switching a robot to another network, and the panel following it to its new address
    code, j = http("/api/net_move", {"network": "Competition Hall", "host": ROBOTS[1]})
    check("switching the second robot, with the remembered password", code == 200 and j["ok"], j)
    check("the robot got it (its setup page now shows the new network)", "value='Competition Hall'" in page(ROBOTS[1]))
    check("the first robot wasn't touched", "value='Classroom'" in page(ROBOTS[0]))
    log = [e["text"] for e in http("/state.json?since=0")[1]["events"]]
    check("the log says what happened, without the password", any("Competition Hall" in t for t in log)
          and not any("goteam2026" in t or "not-a-real-one" in t for t in log), log[-2:])
    check("the panel looks for it on the network", state()["looking_for"] == ["Rocket"], state()["looking_for"])
    mocks[ROBOTS[1]].terminate(); mocks[ROBOTS[1]].wait(5)  # it leaves…
    mocks[MOVED] = simulator(MOVED)  # …and turns up at a new address
    check("…and finds it at its new address, by its hardware address", wait_for(lambda: player("Rocket").get("host") == MOVED
          and not state()["looking_for"], 25), (player("Rocket"), state()["looking_for"]))

    code, j = http("/api/net_move", {"network": "Classroom", "target": "team"})
    check("switching the whole team", code == 200 and j["ok"] and set(j["results"]) == {ROBOTS[0], MOVED}, j)
    check("…every robot got it", all("value='Classroom'" in page(h) for h in (ROBOTS[0], MOVED)))

    # ---- its own hotspot, and back
    code, j = http("/api/net_hotspot", {})
    check("switching the panel's robot to its own hotspot", code == 200 and j["host"] == HOTSPOT and j["remembered"] == "Classroom", j)
    check("the robot got it (Join existing network is off)", "id='f1' type='checkbox' onclick='di()' />" in page(ROBOTS[0]))
    code, j = http("/api/net_info", {})
    check("the panel says to join the robot's hotspot", "join its AIM" in j.get("robot_problem", ""), j)
    mocks[ROBOTS[0]].terminate(); mocks[ROBOTS[0]].wait(5)
    mocks[HOTSPOT] = simulator(HOTSPOT)  # this Mac joins the robot's hotspot
    check("the panel reconnects by itself once this Mac is on the hotspot", wait_for(lambda: state()["robot"]["connected"]
          and state()["robot"]["host"] == HOTSPOT, 25), state()["robot"].get("host"))
    code, j = http("/api/net_move", {"network": "Classroom"})
    check("from the hotspot, switching it to a network", code == 200 and j["ok"], j)
    mocks[HOTSPOT].terminate(); mocks[HOTSPOT].wait(5)  # its hotspot goes…
    mocks[ROBOTS[0]] = simulator(ROBOTS[0])  # …and it joins the network
    check("the panel finds it on the network and reconnects", wait_for(lambda: state()["robot"]["connected"]
          and state()["robot"]["host"] == ROBOTS[0] and not state()["looking_for"], 30), state()["robot"].get("host"))

    # ---- a robot on its own hotspot, put there from its screen: the panel notices, and Use takes it
    mocks[ROBOTS[0]].terminate(); mocks[ROBOTS[0]].wait(5)
    mocks[HOTSPOT] = simulator(HOTSPOT)
    wait_for(lambda: answers(HOTSPOT) and not state()["robot"]["connected"], 10)
    code, j = http("/api/net_info", {})
    check("the panel notices this Mac is on a robot's hotspot", code == 200 and (j.get("hotspot") or {}).get("host") == HOTSPOT, j)
    code, j = http("/api/net_use", {"host": HOTSPOT})
    check("Use that robot", code == 200 and state()["robot"]["connected"] and state()["robot"]["host"] == HOTSPOT, j)
    code, j = http("/api/net_use", {"host": MOVED})
    check("Use on a team robot's address shows that player", code == 200 and state()["player"] == "Rocket", state()["player"])

    for name in ("Classroom", "Competition Hall", "Library Wi-Fi"):
        http("/api/net_forget", {"network": name})
    check("forgetting networks takes their passwords out of the keychain", keychain("Classroom") is None and keychain("Competition Hall") is None
          and keychain("Library Wi-Fi") is None)
finally:
    panel.terminate(); panel.wait(5)
    for m in mocks.values():
        m.terminate(); m.wait(5)
    for leftover in ("Classroom", "Competition Hall", "Library Wi-Fi"):
        subprocess.run(["security", "delete-generic-password", "-a", leftover, "-s", KEYCHAIN], capture_output=True)
    SETUP.unlink(missing_ok=True)

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
