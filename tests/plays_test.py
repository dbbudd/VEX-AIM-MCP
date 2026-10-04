"""The soccer plays against the arena simulator, run in this process so the tests can check where the robot and
the ball really are. Each test starts a fresh simulator (ports 8880-8885), connects, optionally opens a control
panel to build the map (ports 8780-8784), places the robot on the soccer pitch (the simulator starts it at
(0, -700) facing up the field, so the field offset is (0, -700)), unlocks motion and runs plays through
routines.run, as Claude's tools and the panel would.

    ~/.venvs/vex-aim/bin/python test_plays.py [-v] [-p] [test names...]     (-v: show the robot's log, -p: in parallel)
"""
import asyncio
import contextlib
import copy
import logging
import math
import sys
import tempfile
import os
import time
import traceback


from websockets.asyncio.server import serve  # noqa: E402

from pathlib import Path

os.environ.setdefault("AIM_PANEL_SETUP", str(Path(tempfile.gettempdir()) / "aim_test_unused_setup.json"))  # never the real set-up
from vex_aim_mcp import plays  # noqa: E402
from vex_aim_mcp import routines  # noqa: E402
from vex_aim_mcp.aim_client import FLAG_MOVE_ACTIVE, FLAG_MOVING, FLAG_TURN_ACTIVE, AimError, AimRobot  # noqa: E402
from vex_aim_mcp.mock_robot import MockAim, handle_client  # noqa: E402
from vex_aim_mcp.panel import ControlPanel  # noqa: E402
from vex_aim_mcp.world import PanelState  # noqa: E402

MEASURED = {"kick": {"soft": [400.0], "medium": [1340.0], "hard": [3200.0]},  # the simulator's kicks: v²/(2 × 450)
            "launch": {"soft": [600.0], "medium": [1100.0], "hard": [1700.0]},
            "drive": {"60": {"speed_percent": 60, "asked_mm_s": 120, "top_mm_s": 120, "accel_s": 0.2}}}
SOFT_ONLY = {"kick": {"soft": [400.0]}, "launch": {"soft": [600.0]}, "drive": {}}
NOTHING = {"kick": {}, "launch": {}, "drive": {}}
OBSTACLE = (-250.0, 250.0)  # AprilTag 5 in the arena
failures: list[str] = []


def check(test, label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} [{test}] {label}" + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(f"{test}: {label}")


class Watch:
    """The simulator's true robot and ball positions, 50 times a second."""

    def __init__(self, mock: MockAim) -> None:
        self.mock, self.robot, self.ball = mock, [], []

    async def run(self) -> None:
        while True:
            x, y, h = self.mock.field_pose()
            self.robot.append((x, y, h))
            self.ball.append((self.mock.ball[0], self.mock.ball[1], self.mock.ball_held))
            await asyncio.sleep(0.02)

    def crossing(self, y_line: float, start: int = 0) -> float | None:
        """x where the ball first crossed y_line going up the field."""
        b = self.ball[start:]
        for (x0, y0, _), (x1, y1, _) in zip(b, b[1:]):
            if y0 < y_line <= y1:
                return x0 + (x1 - x0) * (y_line - y0) / (y1 - y0)
        return None

    def closest_robot(self, p, start: int = 0) -> float:
        return min(math.dist((x, y), p) for x, y, _ in self.robot[start:])


class BlindMock(MockAim):
    """A camera more like the real robot's: its onboard AI loses the ball within blind_mm of the robot's centre
    (the last ~10 cm in front of the camera) and can't see it in the kicker."""

    def __init__(self, blind_mm: float) -> None:
        super().__init__("arena")
        self.blind_mm = blind_mm

    def _arena_view(self):
        rx, ry, _ = self.field_pose()
        for item in super()._arena_view():
            if item[0] != "SportsBall" or not (self.ball_held or math.dist(self.ball, (rx, ry)) < self.blind_mm):
                yield item


@contextlib.asynccontextmanager
async def arena(sim_port, panel_port=None, ball=(200.0, -100.0), held=False, abilities=MEASURED, team="blue",
                mock=None):
    mock = mock or MockAim("arena")
    mock.ball, mock.ball_held = list(ball), held
    tasks = [asyncio.create_task(mock.ball_loop())]
    server = await serve(lambda ws: handle_client(ws, mock), "127.0.0.1", sim_port, compression=None)
    robot = AimRobot(f"127.0.0.1:{sim_port}")
    state = PanelState(setup_file=None)  # never touch the person's panel_setup.json
    panel = ControlPanel(robot, state, panel_port) if panel_port else None
    watch = Watch(mock)
    try:
        await robot.connect()
        if panel:
            await panel.start(use_yolo=False)
            await asyncio.sleep(0.6)  # its first map update starts the map and the field offset afresh
        state.field.update(name="soccer", offset=[0.0, -700.0], located_by="placed by hand")
        state.team, state.abilities = team, copy.deepcopy(abilities)
        robot.motion_enabled = True
        tasks.append(asyncio.create_task(watch.run()))
        yield mock, robot, state, watch
    finally:
        for t in tasks:
            t.cancel()
        if panel:
            await panel.stop()
        await robot.disconnect()
        server.close()
        await server.wait_closed()


def robot_at(mock, watch=None):
    x, y, h = mock.field_pose()
    return f"robot at ({x:.0f}, {y:.0f}) heading {h:.0f}°"


async def wait_until(cond, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    return False


# ---------------------------------------------------------------------------------------------

async def test_fetch(sim, pan):
    """The ball in view at (200, -100): drive near it, creep up with the camera, end with it in the kicker."""
    T = "fetch"
    async with arena(sim, pan) as (mock, robot, state, watch):
        here = state.on_field(*robot.position)
        check(T, "field coordinates match the simulator", math.dist(here, mock.field_pose()[:2]) < 2, here)
        t0 = time.monotonic()
        text = await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
        check(T, "ends with the ball held", mock.ball_held, f"{text} | {robot_at(mock)} ball {mock.ball}")
        check(T, "says so", text.startswith("Got the ball"), text)
        check(T, "the routine finished (one at a time)", state.routine is None)
        print(f"     {time.monotonic() - t0:.1f} s: {text}")


async def test_fetch_search(sim, pan):
    """The ball behind the robot, with no map: it looks around, finds it and fetches it."""
    T = "fetch: look around"
    async with arena(sim, None, ball=(-350.0, -900.0)) as (mock, robot, state, watch):
        seq = state.event_seq
        text = await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
        log = [e["text"] for e in state.events if e["seq"] > seq]
        check(T, "looked around for it", any("looking around for the ball" in t for t in log), log[:4])
        check(T, "ends with the ball held", mock.ball_held, f"{text} | {robot_at(mock)} ball {mock.ball}")


async def test_fetch_around_obstacle(sim, pan):
    """An obstacle tag (pinned) right between the robot and the ball: go around it to the ball."""
    T = "fetch: around an obstacle"
    async with arena(sim, pan, ball=(-400.0, 650.0)) as (mock, robot, state, watch):
        state.tags[5] = {"role": "obstacle", "pin": list(OBSTACLE), "note": None}
        await routines.run(state, "go to", plays.go_to(robot, state, -100, -300, 60))
        start = len(watch.robot)
        text = await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
        gap = watch.closest_robot(OBSTACLE, start)
        check(T, "ends with the ball held", mock.ball_held, f"{text} | {robot_at(mock)} ball {mock.ball}")
        check(T, "kept the robot clear of the obstacle's keep-out zone (15 cm + robot)", gap > 200, f"{gap:.0f} mm")


async def test_blind_close(sim, pan):
    """A camera like the real one, blind to the ball close up and in the kicker: fetch_ball finishes on an estimate
    and says to check; shoot then carries on as if the ball is held, and scores."""
    T = "blind close up"
    for blind_mm in (180, 230):  # 230: already out of sight from the spot where the camera should take over
        async with arena(sim, None, mock=BlindMock(blind_mm)) as (mock, robot, state, watch):
            text = await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
            check(T, f"blind within {blind_mm} mm: fetch gets the ball", mock.ball_held, f"{text} | {robot_at(mock)}")
            check(T, f"blind within {blind_mm} mm: and says to check, as the camera can't confirm it",
                  "should be in the kicker" in text, text)
            start = len(watch.ball)
            text = await routines.run(state, "shot", plays.shoot(robot, state, 60))
            await asyncio.sleep(3)
            x = watch.crossing(1100, start)
            check(T, f"blind within {blind_mm} mm: shoot carries on as if the ball is held, and scores",
                  "as if the ball was in the kicker" in text and x is not None and abs(x) < 150,
                  f"crossed at {x}; {text}")


async def test_shoot(sim, pan):
    """Blue team, measured kicks: fetch the ball at (200, -100), find the blue goal at the far end (too far to see
    both posts from the ball), dribble closer, line up and kick: the ball must cross y = 1100 between the posts."""
    T = "shoot"
    async with arena(sim, pan) as (mock, robot, state, watch):
        t0 = time.monotonic()
        text = await routines.run(state, "shot", plays.shoot(robot, state, 60))
        await asyncio.sleep(3.5)  # let the ball finish rolling
        x = watch.crossing(1100)
        end = mock.ball
        check(T, "the ball crossed the goal line between the blue posts", x is not None and abs(x) < 150,
              f"crossed at x={x if x is None else round(x)}; ended at ({end[0]:.0f}, {end[1]:.0f})")
        check(T, "it backed away after the kick", not mock.ball_held and "backed away" in text, text)
        print(f"     {time.monotonic() - t0:.1f} s: {text}")


async def test_shoot_unmeasured(sim, pan):
    """No kick measured yet, no map: fetch the ball, find the goal, dribble to about 30 cm out and kick soft."""
    T = "shoot: nothing measured"
    async with arena(sim, None, abilities=NOTHING) as (mock, robot, state, watch):
        seq = state.event_seq
        text = await routines.run(state, "shot", plays.shoot(robot, state, 60))
        log = [e["text"] for e in state.events if e["seq"] > seq]
        await asyncio.sleep(2.5)
        x = watch.crossing(1100)
        end = mock.ball
        check(T, "kicked soft", "Kicked soft" in text, text)
        check(T, "explained why: no kick measured", any("no kick has been measured" in t.lower() for t in log), log)
        in_front = abs(end[0]) < 150 and end[1] > 950
        check(T, "the ball crossed between the posts, or ended in front of the goal",
              (x is not None and abs(x) < 150) or in_front, f"crossed at {x}; ended at ({end[0]:.0f}, {end[1]:.0f})")
        print(f"     {text}")

    # And with the ball already in the kicker at the start, 1.8 m from the goal: too far to see it, no map.
    async with arena(sim, None, held=True, abilities=NOTHING) as (mock, robot, state, watch):
        try:
            await routines.run(state, "shot", plays.shoot(robot, state, 60))
            check(T, "goal out of sight and not on the map: says to explore first", False)
        except AimError as e:
            check(T, "goal out of sight and not on the map: says to explore first", "Exploring the field" in str(e), e)


async def test_shoot_orange_from_held(sim, pan):
    """Orange team, ball in the kicker at the start: the orange goal is behind the robot, 40 cm away."""
    T = "shoot: orange, behind"
    async with arena(sim, None, held=True, team="orange") as (mock, robot, state, watch):
        text = await routines.run(state, "shot", plays.shoot(robot, state, 60))
        await asyncio.sleep(2.5)
        b = watch.ball
        x = next((x0 + (x1 - x0) * (-1100 - y0) / (y1 - y0) for (x0, y0, _), (x1, y1, _) in zip(b, b[1:])
                  if y0 > -1100 >= y1), None)
        check(T, "the ball crossed the orange goal line between its posts", x is not None and abs(x) < 150,
              f"crossed at {x}; {text}")


async def test_pass(sim, pan):
    """Pass from the start to (300, -100), 62 cm away: soft rolls 40 cm, so the gentlest that reaches is medium.
    The ball must roll through the spot."""
    T = "pass"
    async with arena(sim, None, held=True) as (mock, robot, state, watch):
        start = len(watch.ball)
        text = await routines.run(state, "pass", plays.pass_to(robot, state, 300, -100, 60))
        await asyncio.sleep(2.5)
        miss = min(math.dist((x, y), (300, -100)) for x, y, held in watch.ball[start:] if not held)
        check(T, "kicked medium, the gentlest that reaches", "medium kick" in text, text)
        check(T, "the ball rolled through the spot", miss < 50, f"missed by {miss:.0f} mm")


async def test_pass_dribble(sim, pan):
    """Only soft measured (40 cm): a pass to (0, 300), a metre away, dribbles closer first, then stops about there."""
    T = "pass: dribble first"
    async with arena(sim, None, held=True, abilities=SOFT_ONLY) as (mock, robot, state, watch):
        text = await routines.run(state, "pass", plays.pass_to(robot, state, 0, 300, 60))
        await asyncio.sleep(2.5)
        end = mock.ball
        check(T, "dribbled closer, then kicked soft", "dribbled" in text and "soft kick" in text, text)
        check(T, "the ball stopped near the spot (within 10 cm, past it)",
              math.dist(end, (0, 300)) < 100 and end[1] > 280, f"ended at ({end[0]:.0f}, {end[1]:.0f})")


async def test_go_to(sim, pan):
    """go_to a spot: arrive within 5 cm, facing the asked heading; refuse a spot off the pitch."""
    T = "go to"
    async with arena(sim, None) as (mock, robot, state, watch):
        text = await routines.run(state, "go to", plays.go_to(robot, state, 300, 200, 60, heading=90))
        x, y, h = mock.field_pose()
        check(T, "arrived within 5 cm", math.dist((x, y), (300, 200)) < 50, f"({x:.0f}, {y:.0f}) {text}")
        check(T, "facing the asked heading", abs((h - 90 + 180) % 360 - 180) < 3, f"{h:.1f}°")
        try:
            await routines.run(state, "go to", plays.go_to(robot, state, 0, 1300, 60))
            check(T, "refuses a spot off the pitch", False)
        except AimError as e:
            check(T, "refuses a spot off the pitch", "off the soccer pitch" in str(e), str(e))


async def test_go_to_around_obstacle(sim, pan):
    """go_to a spot straight behind the obstacle tag: drive around its keep-out zone."""
    T = "go to: around an obstacle"
    async with arena(sim, None) as (mock, robot, state, watch):
        state.tags[5] = {"role": "obstacle", "pin": list(OBSTACLE), "note": None}
        await routines.run(state, "go to", plays.go_to(robot, state, -250, 650, 60))
        x, y, _ = mock.field_pose()
        gap = watch.closest_robot(OBSTACLE)
        check(T, "arrived within 5 cm", math.dist((x, y), (-250, 650)) < 50, f"({x:.0f}, {y:.0f})")
        check(T, "kept clear of the obstacle (15 cm keep-out + robot)", gap > 200, f"{gap:.0f} mm")


async def test_guard(sim, pan):
    """Blue team, so it guards the orange goal (the one the orange team scores in). It finds the goal behind it,
    stands 25 cm in front facing up the field, and slides to stay on the line from the ball to the goal's centre."""
    T = "guard goal"
    async with arena(sim, None, ball=(300.0, -300.0)) as (mock, robot, state, watch):
        job = asyncio.create_task(routines.run(state, "goalkeeping", plays.guard_goal(robot, state, 30, None, 60)))

        def keeper_error(ball):
            x, y, h = mock.field_pose()
            out = ball[1] + 1100  # in front of the goal line
            want = max(-150, min(150, ball[0] * 250 / out if out > 250 else ball[0]))
            return abs(x - want), abs(y + 850), abs((h + 180) % 360 - 180), want

        ready = await wait_until(lambda: abs(mock.field_pose()[1] + 850) < 30 and robot.motion_enabled
                                 and abs((mock.field_pose()[2] + 180) % 360 - 180) < 5 and not robot.is_moving, 25)
        check(T, "took up position 25 cm in front of the orange goal, facing up the field", ready, robot_at(mock))
        for ball in [(300.0, -300.0), (-250.0, -350.0), (100.0, -650.0), (-60.0, -150.0), (250.0, -400.0)]:
            mock.ball = list(ball)
            await asyncio.sleep(4.5)
            dx, dy, dh, want = keeper_error(ball)
            check(T, f"ball at {ball}: between it and the goal (x {want:.0f} ± 4 cm, on the line)",
                  dx < 40 and dy < 30 and dh < 6, f"{robot_at(mock)}, off by {dx:.0f} mm across, {dy:.0f} mm out")
        x_before = mock.field_pose()[0]
        mock.ball = [0.0, -1170.0]  # behind the goal line: out of sight
        await asyncio.sleep(4.0)
        x = mock.field_pose()[0]
        check(T, "lost sight of the ball: back to the middle", x_before > 60 and abs(x) < 30,
              f"from x {x_before:.0f} to {robot_at(mock)}")
        text = await job
        check(T, "finishes after its time with a summary", text.startswith("Guarded the orange goal for 30 s"), text)
        print(f"     {text}")


async def test_no_field(sim, pan):
    """No field chosen: the plays work in the odometry frame ((0, 0) where the robot connected, y the way it faced),
    which here is the pitch shifted by (0, -700)."""
    T = "no field"
    async with arena(sim, None) as (mock, robot, state, watch):
        state.field.update(name="fit", offset=[0.0, 0.0], located_by=None)
        await routines.run(state, "go to", plays.go_to(robot, state, 200, 300, 60))
        x, y, _ = mock.field_pose()
        check(T, "go_to uses odometry coordinates", math.dist((x, y), (200, -400)) < 50, robot_at(mock))
        text = await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
        check(T, "fetch_ball", mock.ball_held, text)
        start = len(watch.ball)
        text = await routines.run(state, "shot", plays.shoot(robot, state, 60))
        await asyncio.sleep(3)
        x = watch.crossing(1100, start)
        check(T, "shoot scores", x is not None and abs(x) < 150, f"crossed at {x}; {text}")


async def test_stop_bump_lock(sim, pan):
    """STOP (cancel) ends a play and stops the robot; a bump stops a play; only one play at a time; motion lock."""
    T = "stop, bump, lock"
    async with arena(sim, None) as (mock, robot, state, watch):
        job = asyncio.create_task(routines.run(state, "go to", plays.go_to(robot, state, 0, 600, 60)))
        await asyncio.sleep(1.5)
        try:
            await routines.run(state, "shot", plays.shoot(robot, state, 60))
            check(T, "only one play at a time", False)
        except AimError as e:
            check(T, "only one play at a time", "busy" in str(e), str(e))
        routines.cancel(state)
        try:
            await job
            check(T, "STOP ends the play", False)
        except routines.Stopped as e:
            check(T, "STOP ends the play", "stopped (STOP)" in str(e), str(e))
        busy = FLAG_MOVE_ACTIVE | FLAG_MOVING | FLAG_TURN_ACTIVE
        y0 = mock.field_pose()[1]
        await asyncio.sleep(0.6)
        check(T, "and the robot stops", not mock.flags & busy and abs(mock.field_pose()[1] - y0) < 2, robot_at(mock))

        # tell it the robot is 30 cm further back than it is: a drive to y = 105 cm then hits the far wall
        state.field["offset"] = [0.0, -1000.0 - robot.position[1]]
        try:
            await routines.run(state, "go to", plays.go_to(robot, state, 0, 1050, 60))
            check(T, "a bump stops the play", False, robot_at(mock))
        except routines.Stopped as e:
            check(T, "a bump stops the play", "bumped" in str(e), str(e))

        state.mode = "driver"
        try:
            await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
            check(T, "Driver mode (the person drives): plays refuse", False)
        except AimError as e:
            check(T, "Driver mode (the person drives): plays refuse", "Driver mode" in str(e), str(e))
        state.mode = "auto"

        robot.motion_enabled = False
        seq = state.event_seq
        try:
            await routines.run(state, "fetch", plays.fetch_ball(robot, state, 60))
            check(T, "motion locked: plays refuse", False)
        except AimError as e:
            check(T, "motion locked: plays refuse before doing anything", "Motion is locked" in str(e)
                  and not [e for e in state.events if e["seq"] > seq and e["who"] == "robot"], str(e))


TESTS = [test_fetch, test_fetch_search, test_fetch_around_obstacle, test_blind_close, test_shoot, test_shoot_unmeasured,
         test_shoot_orange_from_held, test_pass, test_pass_dribble, test_go_to, test_go_to_around_obstacle,
         test_guard, test_no_field, test_stop_bump_lock]
SIM_PORTS, PANEL_PORTS = range(8880, 8886), range(8780, 8785)


async def run_one(test, sim, pan):
    try:
        await asyncio.wait_for(test(sim, pan), 240)
    except Exception as e:
        check(test.__name__, f"ran without an error: {type(e).__name__}: {e}", False)
        traceback.print_exc()


async def main(names, parallel):
    tests = [t for t in TESTS if not names or t.__name__ in names or t.__name__.removeprefix("test_") in names]
    t0 = time.monotonic()
    if parallel:  # in batches, each test with its own simulator and panel port
        for i in range(0, len(tests), len(PANEL_PORTS)):
            batch = tests[i:i + len(PANEL_PORTS)]
            await asyncio.gather(*(run_one(t, SIM_PORTS[j], PANEL_PORTS[j]) for j, t in enumerate(batch)))
    else:
        for t in tests:
            await run_one(t, SIM_PORTS[0], PANEL_PORTS[0])
    print(f"\n{len(tests)} tests in {time.monotonic() - t0:.0f} s")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "-v" in args:
        logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="     %(name)s: %(message)s")
        for quiet in ("mock_aim", "websockets", "vex_aim.client"):
            logging.getLogger(quiet).setLevel(logging.WARNING)
    asyncio.run(main([a for a in args if not a.startswith("-")], "-p" in args))
    print("ALL PASSED" if not failures else f"{len(failures)} FAILED:\n  " + "\n  ".join(failures))
    sys.exit(1 if failures else 0)
