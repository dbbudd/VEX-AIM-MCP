"""Soccer plays: short jobs the robot does by itself, for the assistant's tools and the control panel.

- fetch_ball: find the ball (in view, on the map, or by looking around), drive to just short of it around
  anything in the way, then creep up on it with the camera until it's in the kicker.
- shoot: get the ball, find our goal (the team's colour), dribble closer if no measured kick would score from
  here, line up between the posts, kick with the gentlest strength that scores, and back away.
- pass_to: kick the ball to a spot with the gentlest measured kick that gets there.
- go_to: drive to a spot, around anything in the way.
- guard_goal: play goalkeeper: stand in front of the goal the other team scores in, and slide sideways to stay
  between the ball and the goal.

Each takes (robot, state, ...), uses field coordinates in mm when a field is chosen (the odometry frame
otherwise), needs motion unlocked, stops on a bump (raising routines.Stopped) and returns a plain-English
summary. Run them through routines.run, so only one runs at a time and STOP ends it:

    summary = await routines.run(state, "shot", plays.shoot(robot, state))

strategy.py makes the decisions (shoot or dribble, which kick); this file does the moving.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable

from . import strategy
from .aim_client import AimError, AimRobot, Detection, held_object, is_held, parse_detections
from .routines import KICKER_MM, Stopped, drive_to, progress, speeds
from .strategy import GOAL_POSTS, OTHER_TEAM, Point, heading_to, toward
from .world import FIELDS, PanelState, approx_distance_mm, cut_off, wrap180

STAGING_MM = 250  # drive to this far short of the ball, then finish with the camera
LOOK_STEP_DEG = 45  # the camera sees about ±34°, so 45° steps overlap
VISION_SETTLE_S = 0.4  # after moving, give the onboard AI this long to catch up
GUARD_MM = 250  # a goalkeeper stands this far in front of the goal line
BACK_AWAY_MM = 80  # after a kick, so the kicker's magnet doesn't catch a ball that rolls back
WALL_MM = 100  # keep the robot's centre this far from the field's walls (its radius is about 70 mm)


# ---------------------------------------------------------------------------------------------
# The plays
# ---------------------------------------------------------------------------------------------

async def fetch_ball(robot: AimRobot, state: PanelState, speed_percent: float = 40) -> str:
    """Get the ball into the kicker: find it (in view, on the map, or by looking around), drive to just short of
    it around anything in the way, then creep up on it, re-aiming with the camera before each step."""
    _ready(robot, state)
    mmps, dps = speeds(speed_percent)
    use_map = True
    for _ in range(3):
        where, ball = await _find_ball(robot, state, dps, use_map)
        if where == "in the kicker":
            return f"The ball is in the kicker. {_pose(robot, state)}"
        if ball is None:
            raise AimError("Couldn't see the ball anywhere around the robot (the camera spots it up to about a metre "
                           "away). If it's already in the kicker, the camera may just be missing it: check with look().")
        here = _here(robot, state)
        progress(state, f"fetch: the ball is {math.dist(here, ball) / 10:.0f} cm away ({where})")
        if math.dist(here, ball) > STAGING_MM + 100:
            staging = strategy.approach_spot(state, ball, here, STAGING_MM, WALL_MM)
            await _go(robot, state, staging, mmps, dps, avoid_ball=False)
            await _face(robot, heading_to(_here(robot, state), ball), dps)
        got = await _creep_up(robot, state, mmps, dps, ball)
        if got:
            return f"Got the ball: it's in the kicker. {_pose(robot, state)}"
        if got is None:
            return ("The ball should be in the kicker, but the camera can't see it this close: check with look(). "
                    + _pose(robot, state))
        use_map = False  # it wasn't where the map said: look around instead
        progress(state, "fetch: lost sight of the ball, looking again")
    raise AimError(f"Couldn't get hold of the ball after three tries. {_pose(robot, state)}")


async def shoot(robot: AimRobot, state: PanelState, speed_percent: float = 40, goal: str | None = None) -> str:
    """Score: get the ball (fetching it if the camera sees it somewhere else), find our goal (two barrels of the
    team's colour: like VEX alliances, a team scores in its own colour's goal), dribble closer if no measured
    kick would score from here, line up between the posts with the camera, kick with the gentlest strength that
    scores, and back away. goal names the goal's colour instead of the team's."""
    colour = goal or state.team
    if colour not in GOAL_POSTS:
        raise AimError("No team chosen yet, so I don't know which goal to shoot at. Pick a team in the control panel "
                       "(or with set_team), or name the goal's colour.")
    _ready(robot, state)
    mmps, dps = speeds(speed_percent)
    story = [s for s in [await _get_ball(robot, state, speed_percent)] if s]
    for _ in range(4):
        posts = await _find_goal(robot, state, colour, mmps, dps, story)
        advice = strategy.decide(state, (*_here(robot, state), robot.heading), holding=True, posts=posts,
                                 speed_percent=speed_percent, goal=colour)
        progress(state, f"shoot: {advice['why']}")
        if advice["action"] == "shoot":
            break
        await _go(robot, state, tuple(advice["target_mm"]), mmps, dps, avoid_ball=False)
        story.append(f"dribbled {advice['numbers']['dribble_mm'] / 10:.0f} cm to a better spot")
        centre = ((posts[0][0] + posts[1][0]) / 2, (posts[0][1] + posts[1][1]) / 2)
        await _face(robot, heading_to(_here(robot, state), centre), dps)  # to see the posts again from here
    else:
        raise AimError(f"Couldn't get to a spot to shoot from: {advice['why']} {_pose(robot, state)}")
    aimed = await _aim(robot, state, colour, tuple(advice["target_mm"]), dps)
    await _kick(robot, advice["strength"], mmps)
    before = f" First it {' and '.join(story)}." if story else ""
    return (f"Kicked {advice['strength']} at the {colour} goal from {advice['numbers']['goal_mm'] / 10:.0f} cm out "
            f"({aimed}), then backed away.{before} Check the camera to see if it went in. {_pose(robot, state)}")


async def pass_to(robot: AimRobot, state: PanelState, x: float, y: float, speed_percent: float = 40) -> str:
    """Kick the ball to a spot (mm) with the gentlest measured kick that gets there, fetching the ball first if the
    camera sees it somewhere else, and dribbling closer first if no measured kick reaches (or none is measured)."""
    target = (float(x), float(y))
    _check_on_field(state, target)
    _ready(robot, state)
    mmps, dps = speeds(speed_percent)
    story = [s for s in [await _get_ball(robot, state, speed_percent)] if s]
    for _ in range(3):
        here = _here(robot, state)
        plan = strategy.plan_kick(state, here, target)
        if plan["blocked_by"]:
            raise AimError(f"The {plan['blocked_by']} is in the way of that pass. Pick another spot, or go somewhere "
                           f"with a clear path first. {_pose(robot, state)}")
        if plan["strength"]:
            break
        progress(state, f"pass: no measured kick reaches {plan['distance_mm'] / 10:.0f} cm, so dribbling closer")
        spot = toward(target, here, strategy.kick_from_mm(state) + KICKER_MM)
        await _go(robot, state, spot, mmps, dps, avoid_ball=False)
        story.append(f"dribbled {math.dist(here, spot) / 10:.0f} cm closer")
    else:
        raise AimError(f"Couldn't get close enough for a measured kick to reach that spot. {_pose(robot, state)}")
    await _face(robot, heading_to(_here(robot, state), target), dps)
    await _kick(robot, plan["strength"], mmps)
    rolls = f"rolls about {plan['reach_mm'] / 10:.0f} cm" if plan["reach_mm"] else "hasn't been measured"
    before = f" First it {' and '.join(story)}." if story else ""
    return (f"Passed to {_cm(target)}: a {plan['strength']} kick ({rolls}) over {plan['distance_mm'] / 10:.0f} cm, "
            f"then backed away.{before} {_pose(robot, state)}")


async def go_to(robot: AimRobot, state: PanelState, x: float, y: float, speed_percent: float = 40,
                heading: float | None = None) -> str:
    """Drive to a spot (mm), around anything on the map in the way, and stop within a few cm of it. heading, if
    given, is the way to face at the end (0 = up the field)."""
    spot = (float(x), float(y))
    _check_on_field(state, spot)
    _ready(robot, state)
    mmps, dps = speeds(speed_percent)
    progress(state, f"going to {_cm(spot)}")
    await _go(robot, state, spot, mmps, dps)
    if heading is not None:
        await _face(robot, heading, dps)
    off = math.dist(_here(robot, state), spot)
    return f"Arrived at {_cm(spot)}, within {max(1, math.ceil(off / 10))} cm. {_pose(robot, state)}"


async def guard_goal(robot: AimRobot, state: PanelState, seconds: float = 30, goal: str | None = None,
                     speed_percent: float = 40) -> str:
    """Play goalkeeper for a while: stand 25 cm in front of the goal the other team scores in (like VEX alliances,
    each team scores in its own colour's goal), facing up the field, and slide sideways to stay on the line from
    the ball to the middle of the goal, never past a post. goal names the goal's colour instead."""
    colour = goal or OTHER_TEAM.get(state.team or "")
    if colour not in GOAL_POSTS:
        raise AimError("No team chosen yet, so I don't know which goal to guard. Pick a team, or name the goal's "
                       "colour.")
    _ready(robot, state)
    mmps, dps = speeds(speed_percent)
    g = strategy.goal_mouth(state, await _find_goal(robot, state, colour, mmps, dps), _here(robot, state))
    (cx, cy), (fx, fy), (ax, ay) = g["centre"], g["facing"], g["across"]

    def spot(offset: float) -> Point:  # on the guard line, offset mm across from the middle
        return cx + fx * GUARD_MM + ax * offset, cy + fy * GUARD_MM + ay * offset

    progress(state, f"goalkeeper: taking up position in front of the {colour} goal")
    await _go(robot, state, spot(0), mmps, dps)
    outward = heading_to((0, 0), (fx, fy))  # facing away from the goal, to watch the field
    await _face(robot, outward, dps)
    offset, slides, nearest, last_seen, settle = 0.0, 0, None, time.monotonic(), 0.0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        dets = await _look(robot, settle)
        if held_object(dets) == "SportsBall":
            return f"Caught the ball while guarding the {colour} goal: it's in the kicker. {_pose(robot, state)}"
        if ball := _spot_of(robot, state, dets, "SportsBall"):
            last_seen, offset = time.monotonic(), strategy.keeper_offset(g, ball, GUARD_MM)
            nearest = math.dist(ball, g["centre"]) if nearest is None else min(nearest, math.dist(ball, g["centre"]))
        elif time.monotonic() - last_seen > 2:
            offset = 0.0  # lost sight of the ball: back to the middle, where it sees the most of the field
        settle = 0.0
        if math.dist(_here(robot, state), spot(offset)) > 25:
            progress(state, f"goalkeeper: sliding to {offset / 10:+.0f} cm from the middle", log=False)
            await _slide(robot, state, spot(offset), mmps)
            slides, settle = slides + 1, VISION_SETTLE_S
        if abs(wrap180(outward - robot.heading)) > 5:
            await _face(robot, outward, dps)
            settle = VISION_SETTLE_S
    ball_text = (f"The ball came within {nearest / 10:.0f} cm of the goal." if nearest is not None
                 else "It never saw the ball.")
    return (f"Guarded the {colour} goal for {seconds:.0f} s from {GUARD_MM / 10:.0f} cm in front of it, sliding "
            f"sideways {slides} time{'' if slides == 1 else 's'} to stay between the ball and the goal. {ball_text} "
            + _pose(robot, state))


# ---------------------------------------------------------------------------------------------
# Finding things
# ---------------------------------------------------------------------------------------------

async def _look(robot: AimRobot, settle: float = VISION_SETTLE_S) -> list[Detection]:
    """What the onboard AI sees, once it has caught up with the last move."""
    await asyncio.sleep(settle)
    try:
        return parse_detections(await robot.fresh_status())
    except TimeoutError:
        raise AimError("The robot stopped sending updates (out of Wi-Fi range, or asleep?).") from None


def _where(robot: AimRobot, state: PanelState, d: Detection) -> Point | None:
    """Where something the camera sees is on the field, from its bearing and size (None if that can't be told)."""
    mm = approx_distance_mm(d, state)
    if not mm or cut_off(d) or is_held(d):
        return None
    x, y = _here(robot, state)
    a = math.radians(robot.heading + d.bearing)
    return x + mm * math.sin(a), y + mm * math.cos(a)


def _spot_of(robot: AimRobot, state: PanelState, dets: list[Detection], name: str) -> Point | None:
    """Where the nearest (biggest) one of name in view is on the field."""
    return next((xy for d in dets if d.name == name and (xy := _where(robot, state, d))), None)


async def look_around(robot: AimRobot, state: PanelState, name: str, what: str,
                      enough: Callable[[list[Point]], bool], dps: float) -> list[Point]:
    """Turn on the spot in 45° steps (a full circle at most), noting where each name in view is, until
    enough(places) is true. Something seen in two overlapping views (same heading within 5°) counts once, placed
    from the view where it was nearest the middle of the picture."""
    seen: list[dict] = []
    for step in range(360 // LOOK_STEP_DEG):
        if step:
            await _turn(robot, LOOK_STEP_DEG, dps)
        progress(state, f"looking around for {what}", log=step == 0)
        for d in await _look(robot):
            if d.name != name or not (xy := _where(robot, state, d)):
                continue
            h = (robot.heading + d.bearing) % 360
            same = next((s for s in seen if abs(wrap180(s["heading"] - h)) < 5), None)
            if same is None:
                seen.append({"heading": h, "bearing": d.bearing, "xy": xy})
            elif abs(d.bearing) < abs(same["bearing"]):
                same.update(heading=h, bearing=d.bearing, xy=xy)
        if enough([s["xy"] for s in seen]):
            break
    return [s["xy"] for s in seen]


async def _find_ball(robot: AimRobot, state: PanelState, dps: float, use_map: bool = True) -> tuple[str, Point | None]:
    """Where the ball is: ("in the kicker", None), ("in view" | "on the map" | "found by looking around", spot),
    or ("nowhere", None)."""
    dets = await _look(robot)
    if held_object(dets) == "SportsBall":
        return "in the kicker", None
    if spot := _spot_of(robot, state, dets, "SportsBall"):
        return "in view", spot
    if use_map and (spot := strategy.ball_on_map(state)):
        return "on the map", spot
    places = await look_around(robot, state, "SportsBall", "the ball", lambda places: bool(places), dps)
    return ("found by looking around", places[0]) if places else ("nowhere", None)


async def _find_goal(robot: AimRobot, state: PanelState, colour: str, mmps: float, dps: float,
                     story: list[str] | None = None) -> tuple[Point, Point]:
    """Our goal's two posts on the field: from the camera or the map, else by looking around, and by going
    closer to a lone post to see its partner (the camera sees barrels up to about 1.2 m away)."""
    name, looked = GOAL_POSTS[colour], False
    for _ in range(4):
        in_view = [xy for d in await _look(robot) if d.name == name and (xy := _where(robot, state, d))]
        if posts := strategy.pick_posts(in_view) or strategy.goal_on_map(state, colour):
            return posts
        known = in_view or [(o["x"], o["y"]) for o in strategy.sightings(state, name)]
        if (not known or math.dist(_here(robot, state), known[0]) < 700) and not looked:
            known, looked = await look_around(robot, state, name, f"the {colour} goal",
                                              lambda places: strategy.pick_posts(places) is not None, dps), True
            if posts := strategy.pick_posts(known):
                return posts
        if not known or math.dist(_here(robot, state), known[0]) < 700:
            break  # nothing in sight, or a lone post close by: its partner isn't where it should be
        progress(state, f"only one post of the {colour} goal in sight: going closer to see the other")
        await _go(robot, state, toward(known[0], _here(robot, state), 600), mmps, dps, avoid_ball=False)
        if story is not None:
            story.append(f"went closer to see both posts of the {colour} goal")
        looked = False
    raise AimError(f"Couldn't find the {colour} goal (two {colour} barrels a goal's width apart). Is it set up, and "
                   "within about a metre of the robot? Exploring the field first puts it on the map.")


# ---------------------------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------------------------

async def _move(robot: AimRobot, mm: float, direction: float, mmps: float) -> None:
    """A bounded move in any direction (0 = forward, 90 = right); Stopped if it bumped or didn't finish."""
    outcome = await robot.move(mm, direction % 360, mmps)
    if outcome != "completed":
        raise Stopped(f"A move {outcome}.")


async def _turn(robot: AimRobot, degrees: float, dps: float) -> None:
    """Turn on the spot by an angle (clockwise +); Stopped if it didn't finish."""
    if abs(degrees) < 1:
        return
    outcome = await robot.turn(degrees, dps)
    if outcome != "completed":
        raise Stopped(f"A turn {outcome}.")


async def _face(robot: AimRobot, heading: float, dps: float) -> None:
    """Turn on the spot to a heading (skipping turns under 2°)."""
    if abs(wrap180(heading - robot.heading)) < 2:
        return
    outcome = await robot.turn_to(heading % 360, dps)
    if outcome != "completed":
        raise Stopped(f"A turn {outcome}.")


async def _go(robot: AimRobot, state: PanelState, spot: Point, mmps: float, dps: float,
              avoid_ball: bool = True) -> None:
    """Drive to a spot around anything on the map in the way (facing each waypoint and driving forwards, which
    also dribbles a ball in the kicker), then slide the last few cm. avoid_ball=False: carrying or fetching it."""
    route = strategy.route(state, _here(robot, state), spot, avoid_ball)
    if isinstance(route, str):
        raise AimError(f"Can't find a way to {_cm(spot)}: the {route} is in the way.")
    if len(route) > 1:
        progress(state, f"going around something on the map on the way to {_cm(spot)}")
    for point in route:
        await drive_to(robot, state, point, mmps, dps)
    await _slide(robot, state, spot, mmps)


async def _slide(robot: AimRobot, state: PanelState, spot: Point, mmps: float) -> None:
    """Move straight to a nearby spot without turning: the omni-wheels drive in any direction."""
    here = _here(robot, state)
    if math.dist(here, spot) >= 10:
        await _move(robot, math.dist(here, spot), heading_to(here, spot) - robot.heading, mmps)


def _room(robot: AimRobot, state: PanelState, mm: float) -> float:
    """How far (up to mm) the robot can drive straight ahead before its centre is within WALL_MM of a wall."""
    field = FIELDS.get(state.field["name"])
    if not field:
        return mm
    (x, y), a = _here(robot, state), math.radians(robot.heading)
    for pos, step, half in ((x, math.sin(a), field["w"] / 2 - WALL_MM), (y, math.cos(a), field["l"] / 2 - WALL_MM)):
        if abs(step) > 1e-6:
            mm = min(mm, ((half if step > 0 else -half) - pos) / step)
    return max(mm, 0.0)


async def _creep_up(robot: AimRobot, state: PanelState, mmps: float, dps: float,
                    expected: Point | None = None) -> bool | None:
    """Drive up to the ball, re-aiming with the camera before each step, until it's in the kicker. True if the
    camera sees it there; None if it lost sight of the ball right in front and drove the rest on an estimate (the
    onboard AI often does in the last ~10 cm, so it's probably in the kicker); False if it lost it further away.
    expected is where the ball should be, in case it's already too close to see."""
    here = _here(robot, state)
    # the ball when last seen (or expected): how far, and its bearing from the way the robot faces now
    last_mm, last_bearing = ((math.dist(here, expected), wrap180(heading_to(here, expected) - robot.heading))
                             if expected else (None, 0.0))
    nudged = False
    for _ in range(12):
        held, ball = await _ball_in_view(robot)
        if held:
            return True
        if ball is None:
            if nudged:
                return None
            if last_mm is None or abs(last_bearing) > 8 or last_mm - KICKER_MM > 200:
                return False
            # Lost it right in front, as the onboard AI does: finish on an estimate from its last size.
            if (nudge := _room(robot, state, last_mm - KICKER_MM + 20)) >= 10:
                await _move(robot, nudge, 0, mmps)
            nudged = True
            continue
        last_mm, last_bearing = approx_distance_mm(ball, state), ball.bearing
        if abs(ball.bearing) > 4:
            await _turn(robot, ball.bearing, dps)
            last_bearing = 0.0
            continue
        # all the way in once close (the magnet catches it); from further off, get closer and look again
        step = _room(robot, state, last_mm - KICKER_MM + 20 if last_mm < 350 else min(last_mm - STAGING_MM, 600))
        if step < 5:
            raise AimError("The ball is too close to the wall for the robot to reach without bumping into it. "
                           "Move it out a little.")
        await _move(robot, step, 0, mmps)
        last_mm -= step
    return False


async def _ball_in_view(robot: AimRobot) -> tuple[bool, Detection | None]:
    """Whether the camera sees the ball in the kicker, else the ball in view (the nearest), looking up to three
    times: the onboard AI sometimes misses it for a moment."""
    for _ in range(3):
        dets = await _look(robot)
        if held_object(dets) == "SportsBall":
            return True, None
        if ball := next((d for d in dets if d.name == "SportsBall"), None):
            return False, ball
    return False, None


async def _get_ball(robot: AimRobot, state: PanelState, speed_percent: float) -> str | None:
    """Before a kick: fetch the ball if the camera sees it somewhere other than the kicker. If it can't see it
    at all, carry on as if it's in the kicker (the onboard AI often misses a ball that close)."""
    dets = await _look(robot)
    if held_object(dets) == "SportsBall":
        return None
    if any(d.name == "SportsBall" for d in dets):
        await fetch_ball(robot, state, speed_percent)
        return "fetched the ball"
    return "carried on as if the ball was in the kicker (the camera couldn't see it)"


async def _aim(robot: AimRobot, state: PanelState, colour: str, centre: Point, dps: float) -> str:
    """Face the goal's centre, then fine-tune with the camera while both posts are in view: their bearings are
    more precise than map positions, and their sizes say how far away each is (as in shoot_at_goal)."""
    await _face(robot, heading_to(_here(robot, state), centre), dps)
    tuned = False
    for _ in range(3):
        posts = [d for d in await _look(robot) if d.name == GOAL_POSTS[colour] and not is_held(d) and not cut_off(d)]
        if len(posts) < 2:
            break
        # The posts are K/width away, so the direction to their midpoint is the sum of these (K cancels out).
        x = sum(math.sin(math.radians(robot.heading + d.bearing)) / d.width for d in posts[:2])
        y = sum(math.cos(math.radians(robot.heading + d.bearing)) / d.width for d in posts[:2])
        off, tuned = wrap180(math.degrees(math.atan2(x, y)) - robot.heading), True
        if abs(off) <= 1.5:
            break
        await _turn(robot, off, dps)
    return "lined up between the posts with the camera" if tuned else "aimed by the map: the camera couldn't see both posts"


async def _kick(robot: AimRobot, strength: str, mmps: float) -> None:
    """Kick, then back away so the kicker's magnet doesn't catch the ball if it rolls back."""
    await robot.kick(strength)
    await _move(robot, BACK_AWAY_MM, 180, mmps)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def _here(robot: AimRobot, state: PanelState) -> Point:
    return state.on_field(*robot.position)


def _cm(p) -> str:
    return f"x {p[0] / 10:.0f}, y {p[1] / 10:.0f} cm"


def _pose(robot: AimRobot, state: PanelState) -> str:
    return f"The robot is at {_cm(_here(robot, state))}, heading {round(robot.heading) % 360}°."


def _ready(robot: AimRobot, state: PanelState) -> None:
    """Plays move the robot, so check before starting, rather than part way through, that the person isn't
    driving it (Driver mode, which also cancels a play that's running) and that motion is unlocked."""
    if state.mode == "driver":
        raise AimError("The person is driving the robot (Driver mode in the control panel). Switch to Auto first.")
    if not robot.motion_enabled:
        raise AimError("Motion is locked. Ask the person to confirm the robot is on the floor with clear space "
                       "around it, then unlock motion.")


def _check_on_field(state: PanelState, spot: Point) -> None:
    field = FIELDS.get(state.field["name"])
    if field and not strategy.on_field(state, spot, WALL_MM):
        raise AimError(f"{_cm(spot)} is off the {field['name']}, or too near its walls: x can go from "
                       f"{-(field['w'] / 2 - WALL_MM) / 10:.0f} to {(field['w'] / 2 - WALL_MM) / 10:.0f} cm and y from "
                       f"{-(field['l'] / 2 - WALL_MM) / 10:.0f} to {(field['l'] / 2 - WALL_MM) / 10:.0f} cm.")
