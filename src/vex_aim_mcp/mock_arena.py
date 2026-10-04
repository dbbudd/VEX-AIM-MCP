"""Several pretend VEX AIM robots on one soccer pitch, for trying team play without hardware.

    ~/.venvs/vex-aim/bin/python mock_arena.py                     # 4 robots, on ports 8899-8902
    ~/.venvs/vex-aim/bin/python mock_arena.py --robots 2 --port 8890
    ~/.venvs/vex-aim/bin/python mock_arena.py --starts "[[-200, -700, 0], [0, 700, 180]]"

One process serves every robot, each on its own port (consecutive from --port). They all play in
mock_robot.py's --world arena: a 1.2 x 2.4 m pitch with corner AprilTags, a goal at each end, an
obstacle tag and one ball, all shared. Any robot can pick the ball up by driving into it (only one
holds it at a time) and kick it. A rolling ball bounces off the walls and the robots; a robot catches
it if it rolls gently into its kicker; it scores by rolling between a goal's two barrels. Each robot's
camera sees the others as the onboard AI's "Robot" class, and robots bump into each other.

By default half the robots (rounded up) start at the orange end facing up the field and the rest at
the blue end facing down. --starts sets each one's x and y (mm; (0, 0) is the centre, x to the right,
y up the field) and heading (0 = facing up the field, 180 = facing down).

Test-only commands on /ws_cmd, which a real robot doesn't have: mock_state (where the robot really
is, its screen and lights, the ball and the score) and mock_ball_in_kicker. Every command the robots
receive goes to stderr, labelled with the robot's number.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import sys

from websockets.asyncio.server import ServerConnection, serve

from .aim_client import FLAG_CRASHED, FLAG_MOVE_ACTIVE, FLAG_MOVING, vision_x_for_bearing
from .mock_robot import ARENA, BALL_DECEL, BUSY, COLOURS, DRIVE_ACCEL, MockAim, handle_client, wrap180

log = logging.getLogger("mock_arena")

MAX_ROBOTS = 8
TICK = 0.05  # seconds per step of the simulation, as in mock_robot.py
ROBOT_RADIUS = 70.0  # mm: an AIM robot is about 140 mm across
ROBOT_K = 234 * 140  # px x mm: the camera's focal length times a robot's width (world.py's estimate)
ROBOT_CLASS = 3  # the onboard AI's class id for "Robot"
BALL_K = 8700  # the ball's size constant, as in mock_robot.py
BALL_RADIUS = 20.0
KICKER_REACH = 70.0  # mm from a robot's centre to a ball held in its kicker
CATCH_MM_S = 600.0  # a ball rolling into a kicker slower than this is held by its magnet (a guess)
BATTERIES = (87, 76, 93, 64, 81, 70, 98, 59)  # each robot's battery %, so they're easy to tell apart
# Each goal is the line between its two barrels.
GOALS = {colour: [(x, y) for name, _, x, y, *_ in ARENA["objects"] if name == barrel]
         for colour, barrel in (("blue", "BlueBarrel"), ("orange", "OrangeBarrel"))}
COLOURS.setdefault("Robot", (38, 40, 46))  # mock_robot.py draws the camera's view in these colours


def _project(pose: tuple[float, float, float], things: list[tuple]) -> list[tuple]:
    """mock_robot.py's camera model for the arena. things are (name, class id, x, y, size constant,
    height/width); returns (name, class id, left x, top, width, height) of each one in view, far first."""
    rx, ry, rh = pose
    seen = []
    for name, cid, ox, oy, k, aspect in things:
        d = math.hypot(ox - rx, oy - ry)
        rel = wrap180(math.degrees(math.atan2(ox - rx, oy - ry)) - rh)
        w = k / max(d, 1)
        if d < 60 or abs(rel) > 32 or w < 7:  # out of view, or too small for the AI to pick out
            continue
        w, h = min(w, 300), min(w * aspect, 230)
        x0 = vision_x_for_bearing(rel) - w / 2
        x1, x0 = min(320, x0 + w), max(0, x0)  # partly out of the picture: the box is cut off
        bottom = min(238, 105 + 9000 / d)  # nearer things sit lower in the picture
        seen.append((d, (name, cid, int(x0), int(max(0, bottom - h)), int(x1 - x0), int(h))))
    return [item for _, item in sorted(seen, key=lambda s: -s[0])]


def _crosses(a, b, p, q) -> bool:
    """Whether the step from a to b crosses the line between p and q."""
    def side(u, v, w) -> float:  # which side of the line through u and v the point w is on
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])
    return side(p, q, a) * side(p, q, b) < 0 and side(a, b, p) * side(a, b, q) < 0


def _shared(attr: str) -> property:
    """A robot attribute that lives in the arena, so every robot has (and kicks) the same ball."""
    return property(lambda self: getattr(self.world, attr), lambda self, value: setattr(self.world, attr, value))


class Arena:
    """What the robots share: the ball (and who holds it), the score, and each other."""

    def __init__(self, starts: list[tuple[float, float, float]], first_port: int) -> None:
        self.ball = list(ARENA["ball"])
        self.ball_speed, self.ball_dir = 0.0, 0.0
        self.holder: ArenaRobot | None = None
        self.goals = {"blue": 0, "orange": 0}
        self.robots: list[ArenaRobot] = []
        for i, start in enumerate(starts):
            self.robots.append(ArenaRobot(self, i + 1, start, first_port + i))

    async def ball_loop(self) -> None:
        """Keep the one ball moving for everyone."""
        while True:
            await asyncio.sleep(TICK)
            self.step_ball(TICK)

    def step_ball(self, dt: float = TICK) -> None:
        """Move the ball on by dt seconds: carried in a kicker, rolling, or waiting to be driven into."""
        if self.holder:
            x, y, h = self.holder.field_pose()
            a = math.radians(h)
            self.ball = [x + KICKER_REACH * math.sin(a), y + KICKER_REACH * math.cos(a)]
            return
        if self.ball_speed <= 0:
            if robot := next((r for r in self.robots if r.in_kicker(self.ball)), None):
                self.holder = robot  # driven into: the kicker's magnet holds it
                log.info("robot %d picked up the ball", robot.number)
            return
        before = tuple(self.ball)
        a = math.radians(self.ball_dir)
        self.ball[0] += self.ball_speed * dt * math.sin(a)
        self.ball[1] += self.ball_speed * dt * math.cos(a)
        self.ball_speed = max(0.0, self.ball_speed - BALL_DECEL * dt)
        wx, wy = ARENA["walls"]
        if abs(self.ball[0]) > wx - BALL_RADIUS:  # bounce off a side wall, losing half its speed
            self.ball[0] = math.copysign(wx - BALL_RADIUS, self.ball[0])
            self.ball_dir, self.ball_speed = (-self.ball_dir) % 360, self.ball_speed * 0.5
        if abs(self.ball[1]) > wy - BALL_RADIUS:
            self.ball[1] = math.copysign(wy - BALL_RADIUS, self.ball[1])
            self.ball_dir, self.ball_speed = (180 - self.ball_dir) % 360, self.ball_speed * 0.5
        self._meet_robots(before)
        for colour, (post, other_post) in GOALS.items():
            if abs(self.ball[1]) > abs(before[1]) and _crosses(before, self.ball, post, other_post):
                self.goals[colour] += 1
                log.info("Goal! The ball went into the %s goal (blue %d, orange %d)",
                         colour, self.goals["blue"], self.goals["orange"])

    def _meet_robots(self, before: tuple[float, float]) -> None:
        """A rolling ball that runs into a robot: caught if it rolls gently into the kicker, otherwise
        it bounces off, losing half its speed."""
        for robot in self.robots:
            x, y, _ = robot.field_pose()
            gap = math.hypot(self.ball[0] - x, self.ball[1] - y)
            if gap >= ROBOT_RADIUS + BALL_RADIUS or gap >= math.hypot(before[0] - x, before[1] - y):
                continue  # not touching, or rolling away from it (as from the robot that kicked it)
            if robot.in_kicker(self.ball) and self.ball_speed < CATCH_MM_S:
                self.holder, self.ball_speed = robot, 0.0
                log.info("robot %d caught the ball", robot.number)
            else:
                normal = math.atan2(self.ball[0] - x, self.ball[1] - y)  # from the robot's centre to the ball
                self.ball_dir = (2 * math.degrees(normal) - self.ball_dir + 180) % 360  # mirrored off its side
                self.ball_speed *= 0.5
                reach = ROBOT_RADIUS + BALL_RADIUS
                self.ball = [x + reach * math.sin(normal), y + reach * math.cos(normal)]
                log.info("the ball bounced off robot %d", robot.number)
            return

    def report(self, robot: ArenaRobot) -> dict:
        """mock_state's reply: what's really going on, for tests."""
        x, y, h = robot.field_pose()
        return {"robot": robot.number, "port": robot.port, "x": round(x, 1), "y": round(y, 1),
                "heading": round(h, 1), "screen": robot.screen, "lights": robot.lights,
                "holding_ball": self.holder is robot,
                "ball": {"x": round(self.ball[0], 1), "y": round(self.ball[1], 1), "speed": round(self.ball_speed),
                         "held_by": self.holder.number if self.holder else None},
                "goals": dict(self.goals)}


class ArenaRobot(MockAim):
    """One robot in a shared Arena: its own pose, screen and lights, the arena's ball, and the other
    robots in its camera's view."""

    ball = _shared("ball")
    ball_speed = _shared("ball_speed")
    ball_dir = _shared("ball_dir")

    def __init__(self, world: Arena, number: int, start: tuple[float, float, float], port: int) -> None:
        self.world, self.number, self.start, self.port = world, number, start, port  # first: MockAim sets the ball
        super().__init__("arena")
        self.battery = BATTERIES[(number - 1) % len(BATTERIES)]
        self.screen: dict = {"background": [0, 0, 0], "font": None, "lines": [], "emoji": None}
        self.lights: dict[str, list[int]] = {}

    @property
    def ball_held(self) -> bool:
        return self.world.holder is self

    @ball_held.setter
    def ball_held(self, held: bool) -> None:
        if held:
            self.world.holder = self
        elif self.world.holder is self:
            self.world.holder = None

    def field_pose(self) -> tuple[float, float, float]:
        """Where the robot is on the pitch: x, y (mm) and heading (0 = up the field), from its start.
        Its raw heading starts at START_HEADING, as in mock_robot.py."""
        sx, sy, sh = self.start
        a = math.radians(sh - self.START_HEADING)
        return (sx + self.x * math.cos(a) + self.y * math.sin(a), sy + self.y * math.cos(a) - self.x * math.sin(a),
                (self.heading - self.START_HEADING + sh) % 360)

    def near_side(self, viewer: tuple[float, float, float]) -> tuple[float, float]:
        """The middle of this robot's side facing the viewer: what a camera's distance by size measures to."""
        x, y, _ = self.field_pose()
        d = max(math.hypot(viewer[0] - x, viewer[1] - y), 1)
        return x + (viewer[0] - x) * ROBOT_RADIUS / d, y + (viewer[1] - y) * ROBOT_RADIUS / d

    def in_kicker(self, ball: list[float]) -> bool:
        """Whether the ball is right in front of the kicker, where its magnet holds it (mock_robot.py's rule)."""
        x, y, h = self.field_pose()
        rel = wrap180(math.degrees(math.atan2(ball[0] - x, ball[1] - y)) - h)
        return math.hypot(ball[0] - x, ball[1] - y) < 100 and abs(rel) < 15

    def _arena_view(self):
        """What the camera sees: the arena as in mock_robot.py, plus the other robots and a ball held
        by one of them, in front of it."""
        pose = self.field_pose()
        things = list(ARENA["objects"])
        if not self.ball_held:
            things.append(("SportsBall", 0, *self.world.ball, BALL_K, 1.0))
        for other in self.world.robots:
            if other is not self:
                things.append(("Robot", ROBOT_CLASS, *other.near_side(pose), ROBOT_K, 1.0))
        yield from _project(pose, things)
        if self.ball_held:
            yield "SportsBall", 0, 88, 191, 145, 49  # low and centred, like a real ball in the kicker

    async def _drive(self, distance: float, angle: float, speed: float) -> None:
        """mock_robot.py's drive, except that other robots stop it with a bump too (both feel it)."""
        self.flags |= FLAG_MOVE_ACTIVE | FLAG_MOVING
        try:
            theta = math.radians(self.heading + angle)
            sign, left, v = math.copysign(1, distance), abs(distance), 0.0
            while left > 0:
                await asyncio.sleep(TICK)
                v = min(max(speed, 1), v + DRIVE_ACCEL * TICK)
                step = min(left, v * TICK)
                left -= step
                dx, dy = sign * step * math.sin(theta), sign * step * math.cos(theta)
                before = self.field_pose()
                self.x, self.y = self.x + dx, self.y + dy
                if hit := self._ran_into(before):
                    self.x, self.y = self.x - dx, self.y - dy
                    self._bumped(hit)
                    break
        finally:
            self.flags &= ~BUSY

    async def _free_drive(self, sideways: float, forwards: float, spin: float) -> None:
        """mock_robot.py's driver control (spin_wheels), except that other robots stop it too."""
        self.flags |= FLAG_MOVING
        try:
            while True:
                await asyncio.sleep(TICK)
                theta = math.radians(self.heading)
                dx = (forwards * math.sin(theta) + sideways * math.sin(theta + math.pi / 2)) * TICK
                dy = (forwards * math.cos(theta) + sideways * math.cos(theta + math.pi / 2)) * TICK
                before = self.field_pose()
                self.x, self.y = self.x + dx, self.y + dy
                self.heading = (self.heading + spin * TICK) % 360
                if hit := self._ran_into(before):  # back off the step and bump, but keep going
                    self.x, self.y = self.x - dx, self.y - dy
                    self._bumped(hit)
        finally:
            self.flags &= ~BUSY

    def _ran_into(self, before: tuple[float, float, float]) -> ArenaRobot | str | None:
        """What the last step ran into: "a wall", another robot, or nothing."""
        x, y, _ = self.field_pose()
        wx, wy = ARENA["walls"]
        if abs(x) > wx - 80 or abs(y) > wy - 80:  # the robot's edge reached a wall
            return "a wall"
        for other in self.world.robots:
            if other is not self:
                ox, oy, _ = other.field_pose()
                gap = math.hypot(x - ox, y - oy)
                if gap < 2 * ROBOT_RADIUS and gap < math.hypot(before[0] - ox, before[1] - oy):
                    return other
        return None

    def _bumped(self, hit: ArenaRobot | str) -> None:
        """Ran into a wall or a robot: both robots feel it."""
        if isinstance(hit, ArenaRobot):
            if not self.flags & FLAG_CRASHED:  # once per bump, not every step of pushing
                log.info("robot %d bumped into robot %d", self.number, hit.number)
            hit.bump()
        self.bump()

    def bump(self) -> None:
        """Set the crash flag for a moment, as a real robot does when it's knocked."""
        self.flags |= FLAG_CRASHED
        asyncio.get_running_loop().call_later(0.6, lambda: setattr(self, "flags", self.flags & ~FLAG_CRASHED))

    def status(self) -> dict:
        s = super().status()
        s["robot"]["battery"] = self.battery
        return s

    def handle(self, cmd: dict) -> dict:
        log.info("robot %d: %s", self.number, json.dumps(cmd))
        if cmd.get("cmd_id") == "mock_state":  # for tests only: a real robot has no such command
            return {"cmd_id": "mock_state", "status": "complete", **self.world.report(self)}
        held = self.ball_held
        reply = super().handle(cmd)
        if reply.get("status") in ("complete", "in_progress"):
            self._remember(cmd)
        if held and not self.ball_held:
            log.info("robot %d kicked the ball", self.number)
        return reply

    def _remember(self, cmd: dict) -> None:
        """Keep track of the screen and lights, for mock_state."""
        cid = cmd.get("cmd_id")
        if cid == "lcd_clear_screen":
            self.screen.update(background=[cmd.get("r", 0), cmd.get("g", 0), cmd.get("b", 0)], lines=[])
        elif cid == "lcd_set_font":
            self.screen["font"] = cmd.get("fontname")
        elif cid in ("lcd_print", "lcd_print_at"):
            self.screen["lines"].append(str(cmd.get("string", "")))
        elif cid == "show_emoji":
            self.screen["emoji"] = cmd.get("name")
        elif cid == "hide_emoji":
            self.screen["emoji"] = None
        elif cid == "light_set":
            for led, rgb in cmd.items():
                if isinstance(rgb, dict):
                    for name in ([f"light{n}" for n in range(1, 7)] if led == "all" else [led]):
                        self.lights[name] = [rgb.get("r", 0), rgb.get("g", 0), rgb.get("b", 0)]

    def describe_start(self) -> str:
        x, y, h = self.start
        facing = {0.0: "facing up the field", 180.0: "facing down the field"}.get(h, f"at heading {h:.0f}°")
        return f"starting at x {x / 10:.0f}, y {y / 10:.0f} cm, {facing}"


def default_starts(count: int) -> list[tuple[float, float, float]]:
    """Half the robots (rounded up) at the orange end facing up the field, the rest at the blue end
    facing down, spread evenly across the pitch."""
    half_width, end = ARENA["walls"][0], abs(ARENA["start"][1])

    def across(n: int) -> list[float]:
        return [-half_width + 2 * half_width * (i + 1) / (n + 1) for i in range(n)]

    return [(x, -end, 0.0) for x in across((count + 1) // 2)] + [(x, end, 180.0) for x in reversed(across(count // 2))]


def read_starts(text: str) -> list[tuple[float, float, float]]:
    """--starts: a JSON list with [x, y, heading] or {"x", "y", "heading"} for each robot."""
    poses = []
    for entry in json.loads(text):
        if isinstance(entry, dict):
            entry = (entry["x"], entry["y"], entry.get("heading", 0))
        x, y, heading = (float(v) for v in (*entry, 0)[:3])
        poses.append((x, y, heading % 360))
    return poses


def starts_problem(starts: list[tuple[float, float, float]]) -> str | None:
    """What's wrong with these start poses, if anything: too many robots, off the pitch, or on top of each other."""
    if not 1 <= len(starts) <= MAX_ROBOTS:
        return f"Give between 1 and {MAX_ROBOTS} robots."
    wx, wy = ARENA["walls"]
    for i, (x, y, _) in enumerate(starts, 1):
        if abs(x) > wx - 80 or abs(y) > wy - 80:
            return f"Robot {i} at ({x:g}, {y:g}) is off the pitch: x must be within ±{wx - 80:g} mm and y within ±{wy - 80:g}."
        for j, (x2, y2, _) in enumerate(starts[:i - 1], 1):
            if math.hypot(x - x2, y - y2) < 2 * ROBOT_RADIUS:
                return f"Robots {j} and {i} would stand on top of each other."
    return None


async def serve_robot(ws: ServerConnection, robot: ArenaRobot) -> None:
    log.info("robot %d: a client opened %s", robot.number, ws.request.path)
    await handle_client(ws, robot)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Pretend to be several VEX AIM robots on one soccer pitch.")
    parser.add_argument("--port", type=int, default=8899, help="the first robot's port; the others follow on (default 8899)")
    parser.add_argument("--robots", type=int, default=4,
                        help=f"how many robots, 1-{MAX_ROBOTS} (default 4: two at each end)")
    parser.add_argument("--starts", metavar="JSON",
                        help='where each robot starts, instead of --robots, e.g. "[[-200, -700, 0], [0, 700, 180]]": '
                             "x and y in mm ((0, 0) is the centre, y up the field) and heading (0 = facing up the field)")
    parser.add_argument("--ball-in-kicker", type=int, metavar="N", help="robot N (1 = the first) starts holding the ball")
    args = parser.parse_args()
    try:
        starts = read_starts(args.starts) if args.starts else default_starts(args.robots)
    except (ValueError, TypeError, KeyError):
        parser.error('--starts must be a JSON list with [x, y, heading] for each robot')
    if problem := starts_problem(starts):
        parser.error(problem)
    if args.ball_in_kicker is not None and not 1 <= args.ball_in_kicker <= len(starts):
        parser.error(f"--ball-in-kicker must be a robot number from 1 to {len(starts)}")

    arena = Arena(starts, args.port)
    if args.ball_in_kicker:
        arena.holder = arena.robots[args.ball_in_kicker - 1]
        arena.step_ball(0)
    ball = asyncio.get_running_loop().create_task(arena.ball_loop())
    async with contextlib.AsyncExitStack() as stack:
        for robot in arena.robots:
            try:
                await stack.enter_async_context(
                    serve(lambda ws, r=robot: serve_robot(ws, r), "127.0.0.1", robot.port, compression=None))
            except OSError as e:
                raise SystemExit(f"Couldn't listen on port {robot.port} ({e.strerror}). "
                                 "Is another simulator using it?") from None
            log.info("robot %d listening on 127.0.0.1:%d, %s", robot.number, robot.port, robot.describe_start())
        await ball



def main() -> None:
    """The vex-aim-arena command."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    logging.getLogger("mock_aim").setLevel(logging.WARNING)  # its lines don't say which robot; ours do
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":
    main()
