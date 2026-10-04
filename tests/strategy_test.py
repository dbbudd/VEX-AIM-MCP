"""Unit tests for strategy.py: decide() and its helpers on hand-made states. No robot, no simulator.

    ~/.venvs/vex-aim/bin/python test_strategy.py
"""
import copy
import math
import sys
import tempfile
import os
import time


from pathlib import Path

os.environ.setdefault("AIM_PANEL_SETUP", str(Path(tempfile.gettempdir()) / "aim_test_unused_setup.json"))  # never the real set-up
from vex_aim_mcp import strategy  # noqa: E402
from vex_aim_mcp.aim_client import AimError  # noqa: E402
from vex_aim_mcp.strategy import decide, roll_time_s  # noqa: E402
from vex_aim_mcp.world import PanelState  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(label)


def close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def entry(name, x, y, kind="ai_object", oid=0, sightings=5, seen=None):
    return {"key": [kind, name, oid], "kind": kind, "name": name, "id": oid, "x": float(x), "y": float(y),
            "seen": time.monotonic() if seen is None else seen, "display": name, "sightings": sightings}


BLUE_GOAL = [entry("BlueBarrel", -150, 1100, oid=1), entry("BlueBarrel", 150, 1100, oid=1)]
ORANGE_GOAL = [entry("OrangeBarrel", -150, -1100, oid=2), entry("OrangeBarrel", 150, -1100, oid=2)]
MEASURED = {"kick": {"soft": [400.0], "medium": [1340.0], "hard": [3200.0]},
            "launch": {"soft": [600.0], "medium": [1100.0], "hard": [1700.0]},
            "drive": {"60": {"speed_percent": 60, "asked_mm_s": 120, "top_mm_s": 118, "accel_s": 0.2}}}
NOTHING = {"kick": {}, "drive": {}, "launch": {}}


def make(team="blue", abilities=MEASURED, objects=(*BLUE_GOAL, *ORANGE_GOAL), tags=None, field="soccer",
         offset=(0.0, 0.0)):
    s = PanelState(setup_file=None)
    s.team = team
    s.field.update(name=field, offset=list(offset), located_by="placed by hand")
    s.abilities = copy.deepcopy(abilities)
    s.map = [dict(o) for o in objects]
    s.tags = tags or {}
    return s


# --- physics: a kicked ball slows down evenly from its launch speed to a stop at its reach
check("roll time: 30 cm of a 40 cm, 60 cm/s kick takes 0.67 s", close(roll_time_s(300, 400, 600), 2 / 3, 1e-6),
      roll_time_s(300, 400, 600))
check("roll time: all of it takes 2R/v = 1.33 s", close(roll_time_s(400, 400, 600), 4 / 3, 1e-6))
check("roll time: past its reach, or launch unknown: None",
      roll_time_s(500, 400, 600) is None and roll_time_s(300, 400, None) is None)
k = strategy.kicks(make(abilities={"kick": {"soft": [400.0], "medium": [1340.0]}, "launch": {"soft": [600.0]},
                                   "drive": {}}))
check("kicks: an unmeasured launch speed comes from the others' slowing (450 mm/s² → medium ≈ 1098 mm/s)",
      list(k) == ["soft", "medium"] and k["soft"]["launch_mm_s"] == 600 and k["medium"]["launch_mm_s"] == 1098, k)
check("drive speed: the fastest tested, with its time to get up to speed",
      strategy.drive_speed(make()) == (118.0, 0.2, True), strategy.drive_speed(make()))
check("drive speed: scaled to another setting (30% asks 60 mm/s, so ≈ 59)",
      close(strategy.drive_speed(make(), 30)[0], 59.0, 0.01), strategy.drive_speed(make(), 30))
check("drive speed: untested, so the asked-for speed (40% = 80 mm/s)",
      strategy.drive_speed(make(abilities=NOTHING), 40) == (80.0, 0.0, False))

# --- picking a goal out of barrel sightings
check("pick_posts: skips a second sighting of the same post",
      strategy.pick_posts([(150, 1100), (160, 1120), (-150, 1100)]) == ((150, 1100), (-150, 1100)))
check("pick_posts: two barrels 2 m apart aren't a goal", strategy.pick_posts([(0, 0), (0, 2000)]) is None)
dup = [entry("BlueBarrel", 150, 1100, sightings=10), entry("BlueBarrel", 170, 1130, sightings=4),
       entry("BlueBarrel", -150, 1100, sightings=6)]
check("goal_on_map: the best-seen pair a goal's width apart",
      strategy.goal_on_map(make(objects=dup), "blue") == ((150, 1100), (-150, 1100)))
g = strategy.goal_mouth(make(), ((-150, -1100), (150, -1100)), (0, -700))
check("goal_mouth: the orange goal faces up the field, 30 cm wide",
      g["centre"] == (0, -1100) and close(g["facing"][1], 1, 1e-9) and close(g["width"], 300, 1e-9), g)
g2 = strategy.goal_mouth(make(field="fit"), ((-150, 1100), (150, 1100)), (0, 2000))
check("goal_mouth: with no field, it faces the robot", close(g2["facing"][1], 1, 1e-9), g2)

# --- the goalkeeper's spot
check("keeper: stays on the line from the ball to the goal's centre (30 cm across, 80 cm out → 9.4 cm at 25 cm)",
      close(strategy.keeper_offset(g, (300, -300), 250), 93.75, 1e-6), strategy.keeper_offset(g, (300, -300), 250))
check("keeper: never past a post", strategy.keeper_offset(g, (-1000, -900), 250) == -150)
check("keeper: a ball nearer the goal line than the keeper: match it across",
      close(strategy.keeper_offset(g, (40, -1000), 250), 40, 1e-6))

# --- fetch
s = make(objects=[*BLUE_GOAL, entry("SportsBall", 0, 300)])
a = decide(s, (0, -700, 0))
check("fetch: not holding, so go and get the ball 1 m away", a["action"] == "fetch" and a["target_mm"] == [0, 300]
      and a["numbers"]["ball_mm"] == 1000 and a["turn_deg"] == 0, a)
check("fetch: drive time at the measured 11.8 cm/s (+ half the 0.2 s to get up to speed) = 8.6 s",
      a["numbers"]["drive_time_s"] == 8.6 and a["numbers"]["path_clear"], a["numbers"])
a = decide(make(), (0, -700, 0))
check("fetch: the ball isn't on the map: look for it", a["action"] == "fetch" and a["target_mm"] is None
      and "isn't on the map" in a["why"], a)
s = make(objects=[entry("SportsBall", 300, 300, seen=100.0), entry("SportsBall", -200, 500, seen=200.0)])
check("fetch: a kicked ball's latest sighting counts, not the old ones", decide(s, (0, 0, 0))["target_mm"] == [-200, 500])
s = make(objects=[entry("SportsBall", 0, 600)], tags={5: {"role": "obstacle", "pin": [0.0, 0.0], "note": None}})
before = copy.deepcopy(s.map)
a = decide(s, (0, -600, 0))
check("fetch: an obstacle between: drive around it (route longer than straight)", not a["numbers"]["path_clear"]
      and a["numbers"]["drive_mm"] > 1200 and "around" in a["why"], a["numbers"])
check("planning doesn't change the map", s.map == before)
check("fetch needs no team", decide(make(team=None, objects=[entry("SportsBall", 0, 0)]), (0, -500, 0))["action"] == "fetch")

# --- shoot
a = decide(make(), (0, 800, 0), holding=True)
check("shoot: 25 cm from the goal (from the kicker): soft rolls 40 cm, enough with 10 cm to spare",
      a["action"] == "shoot" and a["strength"] == "soft" and a["target_mm"] == [0, 1100], a)
check("shoot: the soft ball takes 0.52 s; the numbers show the distance and a clear path",
      a["numbers"]["ball_time_s"] == 0.52 and a["numbers"]["goal_mm"] == 250 and a["numbers"]["path_clear"], a["numbers"])
a = decide(make(), (0, 500, 90), holding=True)
check("shoot: 55 cm away needs medium (soft stops short)", a["action"] == "shoot" and a["strength"] == "medium"
      and a["numbers"]["ball_time_s"] == 0.57 and a["turn_deg"] == -90, a)
check("shoot: explains with the numbers", "55 cm" in a["why"] and "134 cm" in a["why"] and "0.57 s" in a["why"], a["why"])

# --- dribble
a = decide(make(), (0, -500, 0), holding=True)
check("dribble: 155 cm is too far to aim well (90 cm at most)", a["action"] == "dribble" and "too far" in a["why"], a)
check("dribble: to 81 cm out (90% of the 90 cm limit) straight in front of the goal, then medium",
      a["target_mm"] == [0, 240] and a["strength"] == "medium" and a["numbers"]["dribble_mm"] == 740, a)
check("dribble: drive time 0.1 + 74/11.8 = 6.4 s", a["numbers"]["dribble_time_s"] == 6.4, a["numbers"])
s = make(tags={5: {"role": "obstacle", "pin": [0.0, 800.0], "note": None}})
a = decide(s, (0, 500, 0), holding=True)
check("blocked: an obstacle on the shot's line, so no shot", a["action"] == "dribble"
      and a["numbers"]["blocked_by"] == "AprilTag 5" and "AprilTag 5 is in the way" in a["why"], a)
spot = a["target_mm"]
check("blocked: dribble to a spot off to the side with a clear shot", abs(spot[0]) > 300
      and strategy.first_in_the_way(s, strategy.toward(spot, (0, 1100), 50), (0, 1100)) is None, spot)
a = decide(make(), (480, 1000, 0), holding=True)
check("too far to the side: the near post is in the way, so dribble out in front",
      a["action"] == "dribble" and "near post" in a["why"] and abs(a["target_mm"][0]) < 450, a)
check("the goal looks narrower from the side", a["numbers"]["goal_looks_mm"] < 100, a["numbers"])

# --- pass
mate = (100, 700)
s = make(objects=[*BLUE_GOAL, entry("Robot", *mate)])
a = decide(s, (0, -500, 0), holding=True, teammates=[mate])
check("pass: a teammate 1.2 m nearer the goal and within a medium kick", a["action"] == "pass"
      and a["strength"] == "medium" and a["target_mm"] == [100, 700], a)
check("pass: the ball gets there in 1.53 s, the receiver isn't 'in the way'",
      a["numbers"]["pass_time_s"] == 1.53 and a["numbers"]["teammate_nearer_goal_mm"] == 1188, a["numbers"])
a = decide(s, (0, 600, 0), holding=True, teammates=[(0, -300)])
check("no pass to a teammate further from the goal: shoot instead", a["action"] == "shoot", a)
a = decide(make(), (0, -500, 0), holding=True, teammates=[(0, 1000)])
check("pass: a teammate 1.45 m away by the goal: hard (3.2 m) is the gentlest kick that reaches",
      a["action"] == "pass" and a["strength"] == "hard", a)

# --- nothing measured yet
a = decide(make(abilities=NOTHING), (0, 300, 0), holding=True)
check("unmeasured: dribble to 27 cm out, then kick soft", a["action"] == "dribble" and a["strength"] == "soft"
      and a["target_mm"] == [0, 780] and "no kick has been measured" in a["why"].lower(), a)
check("unmeasured: says the driving speed isn't measured either", a["numbers"]["drive_speed_measured"] is False)
a = decide(make(abilities=NOTHING), (0, 820, 0), holding=True)
check("unmeasured but close: shoot soft, and say it's a guess", a["action"] == "shoot" and a["strength"] == "soft"
      and "guess" in a["why"], a)

# --- missing pieces
try:
    decide(make(team=None), (0, 0, 0), holding=True)
    check("no team: holding the ball needs a goal", False)
except AimError as e:
    check("no team: holding the ball needs a goal", "No team" in str(e))
a = decide(make(team="orange", objects=BLUE_GOAL), (0, 0, 0), holding=True)
check("our goal isn't on the map: the shoot play will look for it", a["action"] == "shoot" and a["strength"] is None
      and "isn't on the map" in a["why"], a)
a = decide(make(team=None, objects=BLUE_GOAL), (0, 500, 0), holding=True, goal="blue")
check("the goal can be named instead of the team's", a["action"] == "shoot" and a["numbers"]["goal"] == "blue", a)
s = make(offset=(0.0, -700.0), objects=[entry("BlueBarrel", -150, 1800), entry("BlueBarrel", 150, 1800)])
a = decide(s, (0, 800, 0), holding=True)
check("map entries are in the odometry frame: converted to the field", a["action"] == "shoot"
      and a["target_mm"] == [0, 1100], a)

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
