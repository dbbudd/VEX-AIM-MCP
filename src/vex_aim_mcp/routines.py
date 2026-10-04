"""Longer jobs the robot does by itself, for the control panel and Claude's tools.

- scan_here: turn a full circle in steps, pausing so everything in view goes on the map.
- explore_field: visit a grid of spots across the chosen field, scanning at each, to map the arena.
  It drives around obstacles (and anything else on the map), and skips a spot only if there's no way around.
- kick_test: kick the ball that's in the kicker and watch it roll, to measure how far that strength
  sends it. If it rolls out of the camera's range, the person measures it (record_kick).
- speed_test: drive a measured distance and time it: top speed, and how quickly it gets there.

All of them need motion unlocked and stop on a bump. run() lets only one go at a time, and cancel()
(the STOP button, or Claude's stop tool) ends it whoever started it. Progress goes to the panel's
log and status line. The map itself is built by the control panel while it's open.
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
import time
from collections import Counter
from collections.abc import Coroutine

from .aim_client import DRIVE_MAX_MMPS, TURN_MAX_DPS, AimError, AimRobot, held_object, is_held
from .world import FIELDS, MAP_CONFIRM, OBSTACLE_MM, PanelState, approx_distance_mm, cut_off, wrap180

MAX_SPEED_PERCENT = min(max(float(os.environ.get("AIM_MAX_SPEED_PERCENT", 60)), 5), 100)
MAX_MOVE_MM = float(os.environ.get("AIM_MAX_MOVE_MM", 1000))
SCAN_STEP_DEG = 60  # the camera sees about 68°, so six steps make a full circle with some overlap
SCAN_PAUSE_S = 1.0  # long enough at each step for the map to confirm what's in view
EXPLORE_MARGIN_MM = 300  # stay this far in from the field's edges
EXPLORE_SPACING_MM = 600  # spots at most this far apart: about as far as small objects can be seen
EXPLORE_MAX_SPOTS = 24
CLEARANCE_MM = 100  # keep the robot's path this far from things on the map (obstacles: their keep-out zone)
ROBOT_RADIUS_MM = 70
KICKER_MM = 50  # from the robot's centre to a ball in the kicker
STRENGTHS = ("soft", "medium", "hard")


class Stopped(AimError):
    """The routine ended early: STOP, a bump, or a move that didn't finish."""


def speeds(percent: float) -> tuple[float, float]:
    """Drive (mm/s) and turn (°/s) speeds for a speed %, within the configured cap."""
    p = min(max(percent, 5), MAX_SPEED_PERCENT) / 100
    return p * DRIVE_MAX_MMPS, p * TURN_MAX_DPS


async def run(state: PanelState, name: str, job: Coroutine) -> object:
    """Run one routine at a time. If STOP cancels it, raise Stopped instead of passing the
    cancellation on to whoever is waiting for it."""
    if busy := state.routine_status():
        job.close()
        raise AimError(f"The robot is busy ({busy['name']}: {busy['text']}). Wait for it, or press STOP.")
    task = asyncio.get_running_loop().create_task(job)
    state.routine = {"name": name, "text": "starting", "started": time.time(), "task": task}
    try:
        return await task
    except asyncio.CancelledError:
        if task.cancelled() and not asyncio.current_task().cancelling():
            raise Stopped(f"The {name} was stopped (STOP).") from None
        raise
    finally:
        if state.routine and state.routine["task"] is task:
            state.routine = None


def cancel(state: PanelState) -> bool:
    if (r := state.routine) and not r["task"].done():
        r["task"].cancel()
        return True
    return False


def progress(state: PanelState, text: str, log: bool = True) -> None:
    if state.routine:
        state.routine["text"] = text
    if log:
        state.log("robot", text)


async def _move(robot: AimRobot, mm: float, direction: float, speed_mmps: float) -> None:
    outcome = await robot.move(mm, direction, speed_mmps)
    if outcome != "completed":
        raise Stopped(f"A move {outcome}.")


async def _turn_to(robot: AimRobot, heading: float, speed_dps: float) -> None:
    if abs(wrap180(heading - robot.heading)) < 3:
        return
    outcome = await robot.turn_to(heading, speed_dps)
    if outcome != "completed":
        raise Stopped(f"A turn {outcome}.")


# ---------------------------------------------------------------------------------------------
# Mapping the arena
# ---------------------------------------------------------------------------------------------

async def scan_here(robot: AimRobot, state: PanelState, turn_dps: float, where: str = "here") -> None:
    """A full circle in six steps, pausing at each so the map confirms everything in view."""
    for step in range(360 // SCAN_STEP_DEG):
        progress(state, f"scanning {where}: {step * SCAN_STEP_DEG}°", log=False)
        await asyncio.sleep(SCAN_PAUSE_S)
        outcome = await robot.turn(SCAN_STEP_DEG, turn_dps)
        if outcome != "completed":
            raise Stopped(f"A turn while scanning {outcome}.")


def map_summary(state: PanelState) -> str:
    counts = Counter(o["display"] for o in state.map if o["sightings"] >= MAP_CONFIRM)
    if not counts:
        return "Nothing with a distance is on the map yet."
    return "On the map: " + ", ".join(f"{n} × {name}" if n > 1 else name for name, n in sorted(counts.items())) + "."


def explore_spots(field: dict, here: tuple[float, float]) -> list[tuple[float, float]]:
    """A back-and-forth grid of spots across the field, starting from the end nearest the robot."""
    spacing = EXPLORE_SPACING_MM
    while True:
        def axis(half: float) -> list[float]:
            lo, hi = -half + EXPLORE_MARGIN_MM, half - EXPLORE_MARGIN_MM
            if hi <= lo:
                return [0.0]
            n = max(1, math.ceil((hi - lo) / spacing))
            return [lo + (hi - lo) * i / n for i in range(n + 1)]
        xs, ys = axis(field["w"] / 2), axis(field["l"] / 2)
        if len(xs) * len(ys) <= EXPLORE_MAX_SPOTS:
            break
        spacing *= 1.25
    spots = [(x, y) for j, y in enumerate(ys) for x in (xs if j % 2 == 0 else xs[::-1])]
    return spots if math.dist(here, spots[0]) <= math.dist(here, spots[-1]) else spots[::-1]


def _things_in_the_way(state: PanelState) -> list[tuple[str, float, float, float]]:
    """Everything on the map the robot shouldn't drive through: name, field x, y, keep-away radius."""
    out = []
    for i, t in state.tags.items():
        if t["role"] == "obstacle" and t.get("pin") and state.field["name"] in FIELDS:
            out.append((state.tag_name(i), *t["pin"], OBSTACLE_MM + ROBOT_RADIUS_MM))
    for o in state.map:
        if o["sightings"] < MAP_CONFIRM:
            continue
        x, y = state.on_field(o["x"], o["y"])
        obstacle = o["kind"] == "apriltag" and (state.tags.get(o["id"]) or {}).get("role") == "obstacle"
        out.append((o["display"], x, y, (OBSTACLE_MM if obstacle else CLEARANCE_MM) + ROBOT_RADIUS_MM))
    return out


def _distance_to_path(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def in_the_way(state: PanelState, a: tuple[float, float], b: tuple[float, float]) -> tuple | None:
    """The first thing on the map the straight path a→b would pass too close to: (name, x, y, radius)."""
    for thing in _things_in_the_way(state):
        name, x, y, radius = thing
        gap, here = _distance_to_path(a, b, (x, y)), math.dist(a, (x, y))
        if here < radius and gap >= here - 1:
            continue  # already next to it, but this path takes it no closer: driving away or along is fine
        if gap < radius:
            return thing
    return None


def plan_route(state: PanelState, a: tuple[float, float], b: tuple[float, float], field: dict) -> list | str:
    """Spots to drive through from a to b: straight there, or around whatever is in the way via a point
    beside it. Returns what's in the way (a name) if neither works."""
    blocker = in_the_way(state, a, b)
    if not blocker:
        return [b]
    name, x, y, radius = blocker
    length = math.dist(a, b) or 1
    nx, ny = -(b[1] - a[1]) / length, (b[0] - a[0]) / length  # across the path
    half_w, half_l = field["w"] / 2 - 150, field["l"] / 2 - 150
    vias = [(x + side * nx * (radius + 120), y + side * ny * (radius + 120)) for side in (1, -1)]
    vias = [v for v in vias if abs(v[0]) < half_w and abs(v[1]) < half_l
            and not in_the_way(state, a, v) and not in_the_way(state, v, b)]
    if not vias:
        return name
    return [min(vias, key=lambda v: math.dist(a, v) + math.dist(v, b)), b]


async def drive_to(robot: AimRobot, state: PanelState, spot: tuple[float, float], drive_mmps: float,
                   turn_dps: float, max_leg_mm: float = MAX_MOVE_MM) -> None:
    """Turn towards a spot on the field and drive there, re-aiming after each leg."""
    for _ in range(6):
        fx, fy = state.on_field(*robot.position)
        dx, dy = spot[0] - fx, spot[1] - fy
        if math.hypot(dx, dy) < 60:
            return
        await _turn_to(robot, math.degrees(math.atan2(dx, dy)) % 360, turn_dps)
        await _move(robot, min(math.hypot(dx, dy), max_leg_mm), 0, drive_mmps)


async def explore_field(robot: AimRobot, state: PanelState, speed_percent: float = 30) -> str:
    """Map the arena: scan where the robot is, then visit spots across the chosen field and scan at each."""
    drive_mmps, turn_dps = speeds(speed_percent)
    field = FIELDS.get(state.field["name"])
    if not field:
        progress(state, "scanning around the robot")
        await scan_here(robot, state, turn_dps)
        return ("Scanned a full circle. To explore a whole arena, choose its field for the map and place the robot "
                "on it (or pin a marker). " + map_summary(state))
    spots = explore_spots(field, state.on_field(*robot.position))
    progress(state, f"exploring the {field['name']}: {len(spots)} spots to visit")
    await scan_here(robot, state, turn_dps, "where it started")
    visited, skipped = 0, []
    for i, spot in enumerate(spots, 1):
        route = plan_route(state, state.on_field(*robot.position), spot, field)
        if isinstance(route, str):
            skipped.append(f"spot {i} ({route} in the way)")
            progress(state, f"skipping spot {i}: {route} is in the way")
            continue
        progress(state, f"going to spot {i} of {len(spots)} (x {spot[0] / 10:.0f}, y {spot[1] / 10:.0f} cm)"
                 + (" around something in the way" if len(route) > 1 else ""))
        for point in route:
            await drive_to(robot, state, point, drive_mmps, turn_dps)
        await scan_here(robot, state, turn_dps, f"spot {i}")
        visited += 1
    text = f"Explored {visited} of {len(spots)} spots on the {field['name']}"
    return text + (f", skipping {'; '.join(skipped)}" if skipped else "") + ". " + map_summary(state)


# ---------------------------------------------------------------------------------------------
# Experiments, for strategy: how far kicks go and how fast the robot drives
# ---------------------------------------------------------------------------------------------

async def _watch_ball(robot: AimRobot, state: PanelState, start: tuple[float, float], aim: float,
                      samples: list[tuple[float, float]], t0: float, watch_s: float) -> None:
    """Note how far the ball has gone from where it left the kicker, about 10 times a second,
    until it stops or the camera loses it."""
    last_seen = None
    while (now := time.monotonic()) - t0 < watch_s:
        await asyncio.sleep(0.1)
        (rx, ry), h, along = robot.position, robot.heading, None
        for d in robot.detections():
            if d.name != "SportsBall" or is_held(d) or cut_off(d) or not (mm := approx_distance_mm(d, state)):
                continue
            a = math.radians(h + d.bearing)
            bx, by = rx + mm * math.sin(a), ry + mm * math.cos(a)
            if abs(wrap180(math.degrees(math.atan2(bx - start[0], by - start[1])) - aim)) < 25:  # where the kick sent it
                along = math.dist((bx, by), start)
                break
        if along is not None:
            samples.append((now - t0, along))
            last_seen = now
            recent = [s for t, s in samples if t >= samples[-1][0] - 0.8]
            if samples[-1][0] > 0.8 and len(recent) >= 4 and max(recent) - min(recent) < 25 + 0.04 * along:
                return  # it has stopped
        elif last_seen and now - last_seen > 1.0:
            return  # out of sight, probably out of range


def fit_roll(samples: list[tuple[float, float]], slowing: float | None = None) -> tuple[float, float] | None:
    """A rolling ball slows down evenly, so its distance = d0 + v·t − a·t²/2. Fit that (least squares) to
    the sightings, times from the kick, and return (launch speed v, slowing a), per second, or None.
    The floor slows every kick the same, so once a is known (from a kick seen to stop), only v is fitted:
    much steadier when the ball rolls out of view early."""
    if len(samples) < 4:
        return None
    if slowing:  # d + a·t²/2 = d0 + v·t: a straight line in t
        ts, ys = [t for t, _ in samples], [d + slowing * t * t / 2 for t, d in samples]
        mt, my = statistics.fmean(ts), statistics.fmean(ys)
        var = sum((t - mt) ** 2 for t in ts)
        v = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / var if var else 0
        return (v, slowing) if v > 0 else None
    if len(samples) < 6 or samples[-1][0] - samples[0][0] < 0.8:
        return None  # too little of the roll to tell its slowing from noise
    sums = [[sum(t ** (i + j) for t, _ in samples) for j in range(3)] for i in range(3)]
    rhs = [sum(d * t ** i for t, d in samples) for i in range(3)]

    def det(m: list[list[float]]) -> float:
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

    whole = det(sums)
    if not whole:
        return None
    c = [det([[rhs[r] if col == k else sums[r][col] for col in range(3)] for r in range(3)]) / whole for k in range(3)]
    v, a = c[1], -2 * c[2]
    return (v, a) if v > 0 and 100 <= a <= 3000 else None


async def kick_test(robot: AimRobot, state: PanelState, strength: str, ball_confirmed: bool = False,
                    back_away_mm: float = 80, watch_s: float = 5.0) -> dict:
    """Kick the ball in the kicker and measure how far it goes. Saves the distance when the ball
    stops in view; otherwise the result says how far it was seen, for the person to measure."""
    if strength not in STRENGTHS:
        raise AimError(f"strength must be one of {', '.join(STRENGTHS)}")
    if held_object(robot.detections()) != "SportsBall" and not ball_confirmed:
        raise AimError("The camera can't see a ball in the kicker. Put the ball in the kicker (the AI often misses "
                       "a ball that close), then confirm it's there.")
    (x0, y0), aim = robot.position, robot.heading
    start = (x0 + KICKER_MM * math.sin(math.radians(aim)), y0 + KICKER_MM * math.cos(math.radians(aim)))
    samples: list[tuple[float, float]] = []
    progress(state, f"kick test: {strength} kick")
    t0 = time.monotonic()
    watcher = asyncio.get_running_loop().create_task(_watch_ball(robot, state, start, aim, samples, t0, watch_s))
    try:
        await robot.kick(strength)
        if back_away_mm:  # clear of the magnet, in case it rolls back; the camera keeps watching
            await robot.move(back_away_mm, 180, 100)
        progress(state, "kick test: watching the ball roll", log=False)
        await watcher
    finally:
        watcher.cancel()
    result: dict = {"strength": strength, "seen": bool(samples), "stopped_mm": None, "last_seen_mm": None,
                    "launch_mm_s": None, "time": time.time()}
    if samples:
        t_last, d_last = samples[-1]
        recent = [s for t, s in samples if t >= t_last - 0.8]
        if len(recent) >= 4 and max(recent) - min(recent) < 25 + 0.04 * d_last:
            result["stopped_mm"] = round(statistics.median(recent[-4:]))
        result["last_seen_mm"] = round(max(s for _, s in samples))
        floor = state.abilities.get("slowing") or []  # how hard the floor slows the ball, from kicks seen to stop
        if result["stopped_mm"]:
            rolling = [(t, s) for t, s in samples if s < result["stopped_mm"] - 15]
            if fit := fit_roll(rolling):
                state.abilities.setdefault("slowing", []).append(round(fit[1]))
            fit = fit_roll(rolling, statistics.median(state.abilities["slowing"])) if state.abilities.get("slowing") else fit
        else:
            fit = fit_roll(samples, statistics.median(floor) if floor else None)
            if fit:
                result["predicted_mm"] = round(fit[0] ** 2 / (2 * fit[1]))
        if fit:
            result["launch_mm_s"], result["slowing_mm_s2"] = round(fit[0]), round(fit[1])
    state.last_kick = result
    if result["launch_mm_s"]:
        state.abilities.setdefault("launch", {}).setdefault(strength, []).append(float(result["launch_mm_s"]))
    if result["stopped_mm"]:
        record_kick(state, strength, result["stopped_mm"], measured_by="camera")
    else:
        guess = f" From how it was slowing, it probably stopped about {result['predicted_mm'] / 10:.0f} cm out." if result.get("predicted_mm") else ""
        progress(state, f"{strength} kick: " + (f"the ball rolled out of view at about {result['last_seen_mm'] / 10:.0f} cm.{guess} "
                                                 if result["seen"] else "the camera didn't see the ball after the kick. ")
                 + "Measure where it stopped and enter it.")
    return result


def record_kick(state: PanelState, strength: str, distance_mm: float, measured_by: str = "you") -> None:
    if strength not in STRENGTHS:
        raise ValueError(f"strength must be one of {', '.join(STRENGTHS)}")
    if not 20 <= distance_mm <= 20000:
        raise ValueError("the distance must be between 2 cm and 200 m")
    state.abilities["kick"].setdefault(strength, []).append(float(distance_mm))
    if state.last_kick and state.last_kick["strength"] == strength and not state.last_kick["stopped_mm"]:
        state.last_kick["stopped_mm"] = round(distance_mm)
    state.save_setup()
    reach = state.kick_reach()[strength]
    who = "measured by the camera" if measured_by == "camera" else "measured by you"
    state.log("robot" if measured_by == "camera" else "you",
              f"{strength} kick went {distance_mm / 10:.0f} cm ({who}); {strength} kicks now average "
              f"{reach['mm'] / 10:.0f} cm over {reach['tests']} test{'s' if reach['tests'] > 1 else ''}")


async def speed_test(robot: AimRobot, state: PanelState, speed_percent: float = 30, distance_mm: float = 500,
                     return_to_start: bool = True) -> dict:
    """Drive straight ahead a measured distance, timing it: top speed and how fast it gets there."""
    percent = min(max(speed_percent, 5), MAX_SPEED_PERCENT)
    drive_mmps, _ = speeds(percent)
    distance_mm = min(max(distance_mm, 100), MAX_MOVE_MM)
    progress(state, f"speed test: {distance_mm / 10:.0f} cm at {percent:.0f}%")
    samples: list[tuple[float, float]] = []
    (x0, y0), t0 = robot.position, time.monotonic()

    async def sample() -> None:
        while True:
            d = math.dist(robot.position, (x0, y0))
            if not samples or d != samples[-1][1]:  # the position updates with each status snapshot
                samples.append((time.monotonic() - t0, d))
            await asyncio.sleep(0.03)

    sampler = asyncio.get_running_loop().create_task(sample())
    try:
        await _move(robot, distance_mm, 0, drive_mmps)
        await asyncio.sleep(0.3)
    finally:
        sampler.cancel()
    moving = [(t, d) for t, d in samples if d > 3]
    if len(moving) < 3:
        raise AimError("The robot didn't seem to move, so there's nothing to time.")
    travelled = moving[-1][1]
    t_start = moving[0][0] - moving[0][1] / max(drive_mmps, 1)  # it started a little before the first reading
    t_end = next(t for t, d in moving if d >= travelled - 5)
    window = [((d2 - d1) / (t2 - t1), t1) for i, (t1, d1) in enumerate(moving)
              for t2, d2 in moving[i + 1:] if 0.25 <= t2 - t1 <= 0.5]
    run = t_end - t_start  # cruising speed: the middle of the run, which readings jitter less around than a maximum
    middle = [v for v, t in window if t_start + 0.2 * run <= t <= t_start + 0.8 * run]
    top = statistics.median(middle) if middle else max((v for v, _ in window), default=travelled / max(run, 0.01))
    accel = next((t - t_start for v, t in sorted(window, key=lambda w: w[1]) if v >= 0.9 * top), None)
    result = {"speed_percent": percent, "asked_mm_s": round(drive_mmps), "distance_mm": round(travelled),
              "time_s": round(t_end - t_start, 2), "avg_mm_s": round(travelled / max(t_end - t_start, 0.01)),
              "top_mm_s": round(top), "accel_s": round(max(accel, 0.0), 2) if accel is not None else None}
    state.abilities["drive"][f"{percent:.0f}"] = result
    state.save_setup()
    progress(state, f"at {percent:.0f}% the robot drove {travelled / 10:.0f} cm in {result['time_s']} s: top speed "
                    f"{result['top_mm_s'] / 10:.0f} cm/s (asked for {result['asked_mm_s'] / 10:.0f})"
                    + (f", reached in {result['accel_s']} s" if result["accel_s"] is not None else ""))
    if return_to_start:
        progress(state, "speed test: driving back to the start", log=False)
        await _move(robot, travelled, 180, drive_mmps)
    return result
