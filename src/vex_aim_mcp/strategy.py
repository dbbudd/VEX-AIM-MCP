"""Soccer strategy, worked out on paper: no robot I/O here, so it's quick to test and easy to follow.

decide() looks at the map (the ball, the goals, obstacles), where the robot is, and what the experiments
measured (how far each kick strength rolls the ball, how fast the robot drives), and recommends one of:

- fetch: we haven't got the ball, so go and get it.
- shoot: our goal is within reach of a measured kick, close enough to aim well, and nothing's in the way.
- pass: a teammate is nearer the goal, within reach, with a clear path (a kicked ball beats driving).
- dribble: carry the ball to a spot we can shoot from: closer, or around something in the way.

It gives the numbers behind the choice: distances, kick reach, how long the ball takes to roll there (it
slows down evenly from its launch speed to a stop at its reach) and how long driving would take. Plays only
use kick strengths that have been measured (test_kick); with none measured, they dribble close and kick soft.
plays.py does the moving.

Positions are in mm: field coordinates (x to the right, y up the field) when a field is chosen, the robot's
odometry frame otherwise. Headings are degrees clockwise from up the field.
"""

from __future__ import annotations

import copy
import math
import statistics

from .aim_client import AimError
from .routines import KICKER_MM, STRENGTHS, plan_route, speeds
from .world import FIELDS, MAP_CONFIRM, OBSTACLE_MM, PanelState, wrap180

GOAL_POSTS = {"blue": "BlueBarrel", "orange": "OrangeBarrel"}
OTHER_TEAM = {"blue": "orange", "orange": "blue"}
SHOT_MARGIN_MM = 100  # a shot should still be rolling this far past the goal line
MAX_SHOT_MM = 900  # from further away, small aiming errors miss (and the onboard AI barely sees the posts)
MIN_SHOT_MM = 200  # closer than this, the camera can't see both posts to aim
CLOSE_SHOT_MM = 300  # with no kick measured, dribble this close and kick soft
SPARE = 0.9  # plan to kick from 90% of the furthest a kick could reach from, to have some to spare
BALL_CLEARANCE_MM = 80  # a kicked ball must miss things by this much: ball and barrel radius, plus slack
ROBOT_CLEARANCE_MM = 100  # ... and other robots by this much
RECEIVER_MM = 150  # whoever receives a pass is this close to its target, so isn't "in the way"
PASS_GAIN_MM = 300  # only pass to a teammate at least this much nearer the goal
GOAL_WIDTH_MM = (150, 1000)  # two barrels of one colour this far apart can be a goal's posts
EDGE_MM = 150  # keep planned spots this far in from the field's walls
OPEN_FLOOR = {"w": 20000, "l": 20000}  # with no field chosen, plan as if on a big open floor

Point = tuple[float, float]


# ---------------------------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------------------------

def heading_to(a: Point, b: Point) -> float:
    """The heading (degrees clockwise from up the field) from point a to point b."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def toward(a: Point, b: Point, mm: float) -> Point:
    """The point mm from a on the way to b."""
    d = math.dist(a, b) or 1.0
    return a[0] + (b[0] - a[0]) * mm / d, a[1] + (b[1] - a[1]) * mm / d


def miss_distance(a: Point, b: Point, p: Point) -> float:
    """How close the straight path a→b passes to point p."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def on_field(state: PanelState, p: Point, edge: float = EDGE_MM) -> bool:
    """Whether p is on the chosen field, at least edge mm in from its walls (always true with no field)."""
    field = FIELDS.get(state.field["name"])
    return not field or (abs(p[0]) <= field["w"] / 2 - edge and abs(p[1]) <= field["l"] / 2 - edge)


def inside(state: PanelState, p: Point, edge: float = EDGE_MM) -> Point:
    """p, moved in from the chosen field's walls if it's within edge mm of them (unchanged with no field)."""
    field = FIELDS.get(state.field["name"])
    if not field:
        return p
    half_w, half_l = field["w"] / 2 - edge, field["l"] / 2 - edge
    return max(-half_w, min(half_w, p[0])), max(-half_l, min(half_l, p[1]))


# ---------------------------------------------------------------------------------------------
# Reading the map
# ---------------------------------------------------------------------------------------------

def sightings(state: PanelState, name: str) -> list[dict]:
    """Confirmed map entries of one kind of object, in field coordinates, most recently seen first."""
    found = []
    for o in state.map:
        if o["name"] == name and o["sightings"] >= MAP_CONFIRM:
            x, y = state.on_field(o["x"], o["y"])
            found.append({"x": x, "y": y, "seen": o["seen"], "sightings": o["sightings"]})
    return sorted(found, key=lambda o: -o["seen"])


def ball_on_map(state: PanelState) -> Point | None:
    """Where the ball was last seen. A kicked ball leaves older sightings along its way, so the latest counts."""
    found = sightings(state, "SportsBall")
    return (found[0]["x"], found[0]["y"]) if found else None


def pick_posts(places: list[Point]) -> tuple[Point, Point] | None:
    """A goal's two posts among places a barrel of its colour was seen, best first: the first place with a
    partner a goal's width away (two places closer than that are probably the same barrel)."""
    for i, a in enumerate(places):
        for b in places[i + 1:]:
            if GOAL_WIDTH_MM[0] <= math.dist(a, b) <= GOAL_WIDTH_MM[1]:
                return (a[0], a[1]), (b[0], b[1])
    return None


def goal_on_map(state: PanelState, colour: str) -> tuple[Point, Point] | None:
    """The two posts of this colour's goal on the map, from its best-seen barrels."""
    found = sorted(sightings(state, GOAL_POSTS[colour]), key=lambda o: -o["sightings"])
    return pick_posts([(o["x"], o["y"]) for o in found])


def goal_mouth(state: PanelState, posts: tuple[Point, Point], here: Point) -> dict:
    """The goal between two posts: its centre and width, which way it faces (towards the middle of the field,
    or towards the robot when no field is chosen) and which way is across it."""
    (ax, ay), (bx, by) = posts
    width = math.dist(posts[0], posts[1]) or 1.0
    centre = ((ax + bx) / 2, (ay + by) / 2)
    fx, fy = (ay - by) / width, (bx - ax) / width  # at right angles to the goal line
    front = (0.0, 0.0) if FIELDS.get(state.field["name"]) and math.dist(centre, (0, 0)) > 1 else here
    if (front[0] - centre[0]) * fx + (front[1] - centre[1]) * fy < 0:
        fx, fy = -fx, -fy
    return {"centre": centre, "width": width, "facing": (fx, fy), "across": (fy, -fx), "posts": posts}


def things_a_ball_could_hit(state: PanelState,
                            extra: list[tuple[str, Point]] = ()) -> list[tuple[str, float, float, float]]:
    """What a kicked ball could hit: (name, x, y, how far its path must miss it by). Obstacle tags count with their
    keep-out zone; the ball's own (perhaps old) sightings don't count. extra adds things, e.g. the goal's posts.
    (routines.in_the_way is the robot's version, with room for the robot's body.)"""
    out = [(name, x, y, BALL_CLEARANCE_MM) for name, (x, y) in extra]
    if FIELDS.get(state.field["name"]):
        out += [(state.tag_name(i), *t["pin"], OBSTACLE_MM) for i, t in state.tags.items()
                if t.get("role") == "obstacle" and t.get("pin")]
    for o in state.map:
        if o["sightings"] < MAP_CONFIRM or o["name"] == "SportsBall":
            continue
        obstacle = o["kind"] == "apriltag" and (state.tags.get(o["id"]) or {}).get("role") == "obstacle"
        miss = OBSTACLE_MM if obstacle else ROBOT_CLEARANCE_MM if o["name"] == "Robot" else BALL_CLEARANCE_MM
        out.append((o["display"], *state.on_field(o["x"], o["y"]), miss))
    return out


def first_in_the_way(state: PanelState, a: Point, b: Point, extra: list[tuple[str, Point]] = (),
                     receiver_mm: float = 0.0) -> str | None:
    """The first thing a ball kicked from a to b would hit, if any. Things within receiver_mm of b don't count:
    that's whoever receives a pass."""
    for name, x, y, miss in things_a_ball_could_hit(state, extra):
        if receiver_mm and math.dist((x, y), b) < receiver_mm:
            continue
        if miss_distance(a, b, (x, y)) < miss:
            return name
    return None


def route(state: PanelState, a: Point, b: Point, avoid_ball: bool = True) -> list[Point] | str:
    """Waypoints from a to b around things on the map (routines.plan_route), or the name of what's in the way.
    avoid_ball=False leaves the ball's sightings out: the robot is carrying it, or going to get it."""
    field = FIELDS.get(state.field["name"]) or OPEN_FLOOR
    if not avoid_ball:
        state = copy.copy(state)  # a shallow copy with its own map list, so the real map isn't touched
        state.map = [o for o in state.map if o["name"] != "SportsBall"]
    return plan_route(state, a, b, field)


def path_length(a: Point, waypoints: list[Point]) -> float:
    return sum(math.dist(p, q) for p, q in zip([a, *waypoints], waypoints))


def approach_spot(state: PanelState, ball: Point, here: Point, mm: float, edge: float = EDGE_MM) -> Point:
    """Where to stop mm short of the ball before the camera takes over: on the robot's side of it if possible,
    otherwise further round, so that the robot can get there around things on the map and then has a straight,
    clear run at the ball."""
    back = heading_to(ball, here)
    for turn in (0, 30, -30, 60, -60, 90, -90, 135, -135, 180):
        a = math.radians(back + turn)
        spot = inside(state, (ball[0] + mm * math.sin(a), ball[1] + mm * math.cos(a)), edge)
        if isinstance(route(state, here, spot, avoid_ball=False), list) and \
                route(state, spot, ball, avoid_ball=False) == [ball]:
            return spot
    return inside(state, toward(ball, here, mm), edge)  # the ball is hemmed in: try straight at it


# ---------------------------------------------------------------------------------------------
# What the robot can do, as measured by the experiments
# ---------------------------------------------------------------------------------------------

def kicks(state: PanelState) -> dict[str, dict]:
    """The kick strengths measured so far, gentlest first: how far each rolls the ball (median, mm) and how fast
    it leaves the kicker (mm/s). A launch speed that wasn't measured is worked out from how hard the floor slows
    the ball (measured by kick tests, or else from the kicks whose launch speed is known): v = √(2·slowing·reach)."""
    reach = state.kick_reach()
    slowing = state.abilities.get("slowing") or [r["launch_mm_s"] ** 2 / (2 * r["mm"]) for r in reach.values()
                                                 if r["launch_mm_s"]]
    decel = statistics.median(slowing) if slowing else None
    out = {}
    for s in STRENGTHS:
        if r := reach.get(s):
            launch = r["launch_mm_s"] or (math.sqrt(2 * decel * r["mm"]) if decel else None)
            out[s] = {"reach_mm": r["mm"], "launch_mm_s": round(launch) if launch else None}
    return out


def roll_time_s(distance_mm: float, reach_mm: float, launch_mm_s: float | None) -> float | None:
    """How long a kicked ball takes to roll distance_mm, slowing down evenly from launch_mm_s to a stop at
    reach_mm. (Slowing evenly from v to a stop over R takes 2R/v; the first d of it takes 2R(1 - √(1 - d/R))/v.)
    None if it stops short, or its launch speed is unknown."""
    if not launch_mm_s or distance_mm > reach_mm:
        return None
    return 2 * reach_mm * (1 - math.sqrt(1 - max(distance_mm, 0.0) / reach_mm)) / launch_mm_s


def drive_speed(state: PanelState, speed_percent: float | None = None) -> tuple[float, float, bool]:
    """How fast the robot drives: top speed (mm/s), seconds to get up to it, and whether that was measured. From
    the speed test at the nearest tested setting, scaled to speed_percent (the fastest tested if None); without a
    test, the asked-for speed."""
    tests = [t for t in (state.abilities.get("drive") or {}).values() if t.get("top_mm_s") and t.get("asked_mm_s")]
    if not tests:
        return speeds(speed_percent or 40)[0], 0.0, False
    if speed_percent is None:
        best = max(tests, key=lambda t: t["top_mm_s"])
        return float(best["top_mm_s"]), best.get("accel_s") or 0.0, True
    asked = speeds(speed_percent)[0]
    nearest = min(tests, key=lambda t: abs(t["asked_mm_s"] - asked))
    return asked * nearest["top_mm_s"] / nearest["asked_mm_s"], nearest.get("accel_s") or 0.0, True


def plan_kick(state: PanelState, here: Point, target: Point, margin_mm: float = 0.0,
              extra: list[tuple[str, Point]] = (), receiver_mm: float = 0.0) -> dict:
    """A kick from here at target: how far the ball has to roll (from the kicker, once facing the target), the
    gentlest measured strength that rolls margin_mm past it, how long that takes, and what's in the way. With no
    kick measured, only a soft kick from close up (CLOSE_SHOT_MM) is planned."""
    start = toward(here, target, KICKER_MM)
    distance = math.dist(start, target)
    options = kicks(state)
    if options:
        strength = next((s for s, k in options.items() if k["reach_mm"] >= distance + margin_mm), None)
    else:
        strength = "soft" if distance <= CLOSE_SHOT_MM else None
    k = options.get(strength)
    t = roll_time_s(distance, k["reach_mm"], k["launch_mm_s"]) if k else None
    return {"distance_mm": round(distance), "strength": strength, "measured": bool(options),
            "reach_mm": k["reach_mm"] if k else None, "ball_time_s": round(t, 2) if t is not None else None,
            "blocked_by": first_in_the_way(state, start, target, extra, receiver_mm)}


def kick_from_mm(state: PanelState, margin_mm: float = 0.0) -> float:
    """How far from a target the ball should be for a kick to get there with some to spare: 90% of the longest
    measured reach (less margin_mm), and of MAX_SHOT_MM at most, so the aim is good. With no kick measured, 90% of
    CLOSE_SHOT_MM."""
    longest = max((k["reach_mm"] for k in kicks(state).values()), default=None)
    most = CLOSE_SHOT_MM if longest is None else min(MAX_SHOT_MM, longest - margin_mm)
    return SPARE * max(most, 0.0)


def shooting_spot(state: PanelState, goal: dict, here: Point) -> Point:
    """Where to dribble to for a shot: in front of the goal at kick_from_mm (no further out than the ball is now,
    and no nearer than MIN_SHOT_MM), straight out or up to 40° to either side: the nearest such spot with a clear
    shot."""
    (cx, cy), (fx, fy) = goal["centre"], goal["facing"]
    now = math.dist(here, goal["centre"]) - KICKER_MM
    out = max(MIN_SHOT_MM, min(kick_from_mm(state, SHOT_MARGIN_MM), now)) + KICKER_MM
    posts = [("goal post", p) for p in goal["posts"]]
    clear = []
    for angle in (0, 20, -20, 40, -40):
        a = math.radians(angle)
        spot = (cx + out * (fx * math.cos(a) + fy * math.sin(a)), cy + out * (fy * math.cos(a) - fx * math.sin(a)))
        if on_field(state, spot) and not first_in_the_way(state, toward(spot, goal["centre"], KICKER_MM),
                                                          goal["centre"], posts):
            clear.append(spot)
    return min(clear, key=lambda s: math.dist(s, here)) if clear else (cx + out * fx, cy + out * fy)


def keeper_offset(goal: dict, ball: Point, guard_mm: float) -> float:
    """Where a goalkeeper guard_mm in front of the goal should stand to block a straight shot from the ball at the
    goal's centre: mm across from the middle (along goal["across"]), never past a post."""
    (cx, cy), (fx, fy), (ax, ay) = goal["centre"], goal["facing"], goal["across"]
    dx, dy = ball[0] - cx, ball[1] - cy
    out, side = dx * fx + dy * fy, dx * ax + dy * ay  # how far in front of the goal line, and to the side
    offset = side * guard_mm / out if out > guard_mm else side  # similar triangles; if it's level with us, match it
    half = goal["width"] / 2
    return max(-half, min(half, offset))


# ---------------------------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------------------------

def _cm(mm: float) -> str:
    return f"{mm / 10:.0f} cm"


def _xy(p: Point) -> list[int]:
    return [round(p[0]), round(p[1])]


def decide(state: PanelState, pose: tuple[float, float, float], holding: bool = False, ball: Point | None = None,
           posts: tuple[Point, Point] | None = None, teammates: list[Point] = (),
           speed_percent: float | None = None, goal: str | None = None) -> dict:
    """What to do next, and the numbers behind it. pose is the robot's (x, y, heading) on the field; holding says
    whether the ball is in the kicker (the map can't tell). ball, posts (our goal's two posts) and goal (its colour)
    default to the map's and the team's; teammates are spots to pass to; speed_percent is the speed the robot will
    drive at (default: its fastest measured). Returns {"action": "fetch" | "shoot" | "pass" | "dribble",
    "strength", "target_mm", "turn_deg" (to face the target), "why", "numbers"}."""
    here = (pose[0], pose[1])
    speed, accel_s, measured = drive_speed(state, speed_percent)
    numbers: dict = {"robot_mm": _xy(here), "holding": holding,
                     "kick_reach_mm": {s: k["reach_mm"] for s, k in kicks(state).items()},
                     "drive_mm_s": round(speed), "drive_speed_measured": measured}

    def advise(action: str, why: str, target: Point | None = None, strength: str | None = None) -> dict:
        return {"action": action, "strength": strength, "target_mm": _xy(target) if target else None,
                "turn_deg": round(wrap180(heading_to(here, target) - pose[2])) if target else None,
                "why": why, "numbers": numbers}

    def drive_s(mm: float) -> float:  # getting up to speed loses about half the time it takes
        return round(accel_s / 2 + mm / speed, 1)

    if not holding:
        ball = ball or ball_on_map(state)
        if ball is None:
            return advise("fetch", "We haven't got the ball, and it isn't on the map: look around for it, then go "
                                   "and get it.")
        way = route(state, here, ball, avoid_ball=False)
        length = path_length(here, way) if isinstance(way, list) else math.dist(here, ball)
        numbers.update(ball_mm=round(math.dist(here, ball)), drive_mm=round(length), drive_time_s=drive_s(length),
                       path_clear=isinstance(way, list) and len(way) == 1,
                       blocked_by=way if isinstance(way, str) else None)
        detour = " (going around something on the map)" if isinstance(way, list) and len(way) > 1 else ""
        return advise("fetch", f"We haven't got the ball. It's {_cm(math.dist(here, ball))} away{detour}: about "
                               f"{drive_s(length)} s of driving at {_cm(speed)}/s"
                               f"{'' if measured else ' (driving speed not measured yet)'}.", ball)

    colour = goal or state.team
    if colour not in GOAL_POSTS:
        raise AimError("No team chosen yet, so there's no goal to aim at. Pick a team in the control panel "
                       "(or with set_team).")
    posts = posts or goal_on_map(state, colour)
    if not posts:
        return advise("shoot", f"We've got the ball, but the {colour} goal isn't on the map yet. The shoot play "
                               "looks for it first, and dribbles closer if no kick would reach.")
    g = goal_mouth(state, posts, here)
    extra = [("goal post", p) for p in posts]
    shot = plan_kick(state, here, g["centre"], SHOT_MARGIN_MM, extra)
    d = shot["distance_mm"]
    facing_us = abs((here[0] - g["centre"][0]) * g["facing"][0] + (here[1] - g["centre"][1]) * g["facing"][1])
    numbers.update(goal=colour, goal_mm=d, goal_width_mm=round(g["width"]),
                   goal_looks_mm=round(g["width"] * facing_us / max(math.dist(here, g["centre"]), 1)),
                   strength=shot["strength"], ball_time_s=shot["ball_time_s"], path_clear=not shot["blocked_by"],
                   blocked_by=shot["blocked_by"], dribble_there_s=drive_s(d))

    if shot["blocked_by"] == "goal post":
        problem = "we're too far to the side: the near post is in the way"
    elif shot["blocked_by"]:
        problem = f"the {shot['blocked_by']} is in the way"
    elif d > MAX_SHOT_MM:
        problem = f"the {colour} goal is {_cm(d)} away, too far to aim well ({_cm(MAX_SHOT_MM)} at most)"
    elif not shot["strength"]:
        problem = (f"the {colour} goal is {_cm(d)} away, further than any measured kick rolls (with "
                   f"{_cm(SHOT_MARGIN_MM)} to spare)" if shot["measured"] else
                   f"no kick has been measured yet (test_kick), so only a soft kick from {_cm(CLOSE_SHOT_MM)} or closer")
    elif shot["measured"]:
        time_text = (f" The ball gets there in {shot['ball_time_s']} s; dribbling there would take about "
                     f"{drive_s(d)} s." if shot["ball_time_s"] else "")
        return advise("shoot", f"The {colour} goal is {_cm(d)} away and a {shot['strength']} kick rolls about "
                               f"{_cm(shot['reach_mm'])}, with nothing in the way: shoot!{time_text}",
                      g["centre"], shot["strength"])
    else:
        return advise("shoot", f"The {colour} goal is only {_cm(d)} away, with nothing in the way: shoot soft. (No "
                               "kick has been measured yet, so that's a guess.)", g["centre"], "soft")
    problem = problem[0].upper() + problem[1:]

    best = None
    for mate in teammates:
        gain = math.dist(here, g["centre"]) - math.dist(mate, g["centre"])
        p = plan_kick(state, here, mate, receiver_mm=RECEIVER_MM)
        if gain >= PASS_GAIN_MM and p["strength"] and not p["blocked_by"] and (best is None or gain > best[2]):
            best = (mate, p, gain)
    if best:
        mate, p, gain = best
        numbers.update(pass_mm=p["distance_mm"], pass_strength=p["strength"], pass_time_s=p["ball_time_s"],
                       teammate_nearer_goal_mm=round(gain))
        time_text = f" in {p['ball_time_s']} s" if p["ball_time_s"] else ""
        return advise("pass", f"{problem}, but a teammate {_cm(p['distance_mm'])} away is {_cm(gain)} nearer the "
                              f"goal, and a {p['strength']} kick gets the ball there{time_text}: pass to them.",
                      mate, p["strength"])

    spot = shooting_spot(state, g, here)
    way = route(state, here, spot, avoid_ball=False)
    length = path_length(here, way) if isinstance(way, list) else math.dist(here, spot)
    then = plan_kick(state, spot, g["centre"], SHOT_MARGIN_MM, extra)
    numbers.update(dribble_to_mm=_xy(spot), dribble_mm=round(length), dribble_time_s=drive_s(length),
                   dribble_blocked_by=way if isinstance(way, str) else None, then_strength=then["strength"],
                   then_ball_time_s=then["ball_time_s"])
    return advise("dribble", f"{problem}: dribble {_cm(length)} to x {spot[0] / 10:.0f}, y {spot[1] / 10:.0f} cm "
                             f"(about {drive_s(length)} s), then shoot {then['strength'] or 'soft'}.",
                  spot, then["strength"] or "soft")
