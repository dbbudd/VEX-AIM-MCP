"""Team play foundations. fleet.py's roster, connections, labels, field positions and teammate/opponent
identification, end to end against mock_arena.py with 3 simulated robots on ports 8890-8892; plus
in-process checks of mock_arena's shared ball and bumps, and of the roster rules.
Prints PASS/FAIL lines; exits 1 if anything failed. Only ever talks to 127.0.0.1."""

import asyncio
import json
import logging
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

from websockets.asyncio.client import connect as ws_connect  # noqa: E402

os.environ.setdefault("AIM_PANEL_SETUP", str(Path(tempfile.gettempdir()) / "aim_test_unused_setup.json"))  # never the real set-up
from vex_aim_mcp import mock_arena  # noqa: E402
from vex_aim_mcp import screen  # noqa: E402
from vex_aim_mcp.aim_client import FLAG_CRASHED, AimError, Detection, held_object, is_held, vision_x_for_bearing  # noqa: E402
from vex_aim_mcp.fleet import TEAM_RGB, Fleet  # noqa: E402

PY = sys.executable
PORT = 8890  # the arena's robots: 8890, 8891, 8892
NOBODY = 8895  # nothing listens here
OUT = Path(tempfile.gettempdir())
failures: list[str] = []
logging.getLogger("vex_aim.fleet").setLevel(logging.ERROR)  # from_dict's "skipped" warnings are expected here


def check(label: str, ok, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(label)


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def raises(fn, *args, **kwargs) -> str | None:
    """The AimError message fn raises, or None if it doesn't."""
    try:
        fn(*args, **kwargs)
    except AimError as e:
        return str(e)
    return None


# ---------------------------------------------------------------------------------------------
# In process: the roster rules
# ---------------------------------------------------------------------------------------------

async def roster_checks() -> None:
    f = Fleet()
    f.add("Blaze", "10.0.0.1", "orange", (-200, -700, 0))
    f.add("Comet", "10.0.0.2", "Orange", [200, -700])
    f.add("Rex", "10.0.0.3", "blue", {"x": 0, "y": 700, "heading": 180})
    f.add("Sixteen chars ok", "10.0.0.4")
    check("roster: names, hosts, teams and start poses are stored", f.names() == ["Blaze", "Comet", "Rex", "Sixteen chars ok"]
          and f.get("comet").team == "orange" and f.get("comet").start == (200.0, -700.0, 0.0)
          and f.get("rex").start == (0.0, 700.0, 180.0) and f.get("Sixteen chars ok").team is None)
    check("roster: a name over 16 characters is refused", "at most 16" in (raises(f.add, "Seventeen chars!!", "10.0.0.9") or ""))
    check("roster: names are unique, ignoring capitals and spaces", "already a player" in (raises(f.add, "  bLAZE ", "10.0.0.9") or ""))
    check("roster: two players can't share a robot", "already uses" in (raises(f.add, "Dup", "10.0.0.1") or ""))
    check("roster: a team must be blue or orange", "blue or orange" in (raises(f.add, "Teal", "10.0.0.9", "green") or ""))
    check("roster: a start position must be numbers", "x and y in mm" in (raises(f.add, "Odd", "10.0.0.9", None, ("a", 2)) or ""))
    check("roster: lookup ignores capitals and extra spaces", f.find("  rEx ") is f.get("Rex") and f.find("Nobody") is None)
    check("roster: looking up a missing player says who is there",
          "Blaze, Comet, Rex" in (raises(f.get, "Nobody") or ""))
    f.rename("rex", "T-Rex")
    check("roster: rename", f.names()[2] == "T-Rex" and "already a player" in (raises(f.rename, "T-Rex", "comet") or ""))
    check("roster: with several players and none selected, pick() asks which", "Say which player" in (raises(f.pick) or ""))
    f.select("comet")
    check("roster: select() makes pick() choose that player", f.pick().name == "Comet" and f.pick("blaze").name == "Blaze")
    f.get("Blaze").team = None
    data = json.loads(json.dumps(f.to_dict()))
    g = Fleet.from_dict(data)
    check("roster: to_dict/from_dict round trip through JSON", g.to_dict() == f.to_dict(), json.dumps(data[0]))
    messy = data + [{"name": "", "host": "10.0.0.8"}, {"name": "Comet", "host": "10.0.0.8"}, {"host": "10.0.0.8"},
                    {"name": "Bad team", "host": "10.0.0.8", "team": "red"}, "not a player"]
    check("roster: from_dict skips entries that don't make sense", Fleet.from_dict(messy).names() == f.names())
    check("roster: status of a player that never connected", f.get("Blaze").status()["problem"] == "Not connected yet."
          and f.get("Blaze").field_pose() is None)
    old = f.get("Comet")
    new = await f.set_host("comet", "10.0.0.7")
    check("roster: set_host gives a player a fresh robot, keeping its name, team and start",
          new is not old and f.get("Comet") is new and new.host == "10.0.0.7" and new.team == old.team
          and new.start == old.start and f.names()[1] == "Comet")
    try:
        await f.set_host("Comet", "10.0.0.1")
        refused = None
    except AimError as e:
        refused = str(e)
    check("roster: set_host won't share another player's robot", refused and "already uses" in refused and f.get("Comet").host == "10.0.0.7")


# ---------------------------------------------------------------------------------------------
# In process: mock_arena's shared ball and bumps
# ---------------------------------------------------------------------------------------------

async def arena_checks() -> None:
    # Face to face, 150 mm apart: robot 1's ball is in robot 2's kicker zone too.
    a = mock_arena.Arena([(0, -700, 0), (0, -550, 180)], 0)
    r1, r2 = a.robots
    a.holder = r1
    for _ in range(10):
        a.step_ball()
    check("arena: a held ball stays with its robot, even in another robot's kicker",
          a.holder is r1 and r2.in_kicker(a.ball) and dist(a.ball, (0, -630)) < 1)
    r2.handle({"cmd_id": "kick_soft"})
    check("arena: only the robot holding the ball can kick it", a.holder is r1 and a.ball_speed == 0)
    r2.handle({"cmd_id": "mock_ball_in_kicker"})
    check("arena: mock_ball_in_kicker moves the ball; still one holder", r2.ball_held and not r1.ball_held)
    r2.handle({"cmd_id": "kick_soft"})
    held_by = []
    for _ in range(40):
        a.step_ball()
        held_by.append(a.holder)
    check("arena: a gentle kick into a facing robot's kicker is caught (a pass)", a.holder is r1 and held_by[0] is r1)

    # Kicked into open space: never caught again by the kicker, rolls about v²/2a.
    a = mock_arena.Arena([(0, -700, 0)], 0)
    (r,) = a.robots
    a.holder = r
    a.step_ball()
    r.handle({"cmd_id": "kick_medium"})
    start, holders = tuple(a.ball), set()
    for _ in range(200):
        a.step_ball()
        holders.add(a.holder)
    rolled = dist(a.ball, start)
    check("arena: a kicked ball rolls away and isn't caught again by the robot that kicked it",
          holders == {None} and abs(rolled - 1100 ** 2 / (2 * 450)) < 60, f"rolled {rolled:.0f} mm")

    # Kicked into another robot's side: bounces back (and the kicker's magnet catches the rebound).
    a = mock_arena.Arena([(0, -700, 0), (0, -450, 90)], 0)
    r1, r2 = a.robots
    a.holder = r1
    a.step_ball()
    r1.handle({"cmd_id": "kick_medium"})
    dirs = []
    for _ in range(100):
        a.step_ball()
        dirs.append(round(a.ball_dir))
        if a.holder:
            break
    check("arena: a ball kicked into a robot's side bounces off it", 180 in dirs and a.holder is not r2,
          f"then held by robot {a.holder.number if a.holder else None}")

    # Two robots: a goal for the ball rolled between the blue barrels, counted once.
    a = mock_arena.Arena([(0, 700, 0)], 0)
    a.holder = a.robots[0]
    a.step_ball()
    a.robots[0].handle({"cmd_id": "kick_hard"})
    for _ in range(200):
        a.step_ball()
    check("arena: a ball kicked between the blue barrels scores once", a.goals == {"blue": 1, "orange": 0}, str(a.goals))

    # Robots bump into each other, and can drive away again.
    a = mock_arena.Arena([(0, -700, 0), (0, -400, 180)], 0)
    r1, r2 = a.robots
    await r1._drive(300, 0, 200)
    gap = dist(r1.field_pose(), r2.field_pose())
    check("arena: driving into another robot stops with a bump that both feel",
          140 <= gap < 151 and r1.flags & FLAG_CRASHED and r2.flags & FLAG_CRASHED, f"stopped {gap:.0f} mm apart")
    await asyncio.sleep(0.7)
    await r1._drive(-100, 0, 200)
    check("arena: a robot touching another can drive away", not r1.flags & FLAG_CRASHED
          and abs(dist(r1.field_pose(), r2.field_pose()) - gap - 100) < 1)
    # Driver control (spin_wheels: straight ahead at 200 mm/s) into the other robot: held there with a bump.
    a2 = mock_arena.Arena([(0, -700, 0), (0, -400, 180)], 0)
    d1, d2 = a2.robots
    d1.handle({"cmd_id": "spin_wheels", "vel1": 173.2, "vel2": -173.2, "vel3": 0})
    await asyncio.sleep(1.3)
    pushing_gap, crashed = dist(d1.field_pose(), d2.field_pose()), bool(d1.flags & FLAG_CRASHED and d2.flags & FLAG_CRASHED)
    d1.handle({"cmd_id": "drive", "angle": 0, "speed": 0, "stacking_type": 0})
    check("arena: driving another robot by its wheels (spin_wheels) also bumps into it",
          140 <= pushing_gap < 151 and crashed and not d1.flags & mock_arena.BUSY, f"held {pushing_gap:.0f} mm apart")
    robots = [o for o in r1.visible() if o["type"] == 4 and o["id"] == 3]
    face = dist(r1.field_pose(), r2.field_pose()) - 70
    check("arena: robots see each other as the onboard AI's 'Robot', about square, sized by K = 234 x 140",
          len(robots) == 1 and abs(robots[0]["width"] - 234 * 140 / face) <= 1.5
          and abs(robots[0]["width"] - robots[0]["height"]) <= 1, json.dumps(robots))
    check("arena: the camera picture draws robots", r1.frame()[:2] == b"\xff\xd8")


# ---------------------------------------------------------------------------------------------
# End to end: mock_arena.py with 3 robots, a Fleet of 3 players over WebSockets
# ---------------------------------------------------------------------------------------------

async def mock_state(port: int) -> dict:
    async with ws_connect(f"ws://127.0.0.1:{port}/ws_cmd", proxy=None, compression=None) as ws:
        await ws.send(json.dumps({"cmd_id": "mock_state"}).encode())
        return json.loads(await ws.recv())


def wait_for_ports(ports, timeout=8.0) -> bool:
    end = time.monotonic() + timeout
    for port in ports:
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > end:
                    return False
                time.sleep(0.1)
    return True


async def end_to_end() -> None:
    ports = [PORT, PORT + 1, PORT + 2]
    log = open(OUT / "mock_arena.log", "w")
    proc = subprocess.Popen([PY, "-m", "vex_aim_mcp.mock_arena", "--robots", "3", "--port", str(PORT)], cwd=HERE,
                            stdout=subprocess.DEVNULL, stderr=log)
    fleet = Fleet()
    try:
        check("mock_arena: 3 robots listening on consecutive ports", wait_for_ports(ports))
        blaze = fleet.add("Blaze", f"127.0.0.1:{PORT}", "orange", (-200, -700, 0))
        comet = fleet.add("Comet", f"127.0.0.1:{PORT + 1}", "orange", (200, -700, 0))
        rex = fleet.add("Rex", f"127.0.0.1:{PORT + 2}", "blue", {"x": 0, "y": 700, "heading": 180})
        truth = [await mock_state(p) for p in ports]
        check("mock_arena: default layout for 3 is two at the orange end facing up, one at the blue end facing down",
              [(s["x"], s["y"], s["heading"]) for s in truth] == [(-200, -700, 0), (200, -700, 0), (0, 700, 180)])

        # A fourth player whose "robot" (a local server on NOBODY) accepts the connection but never answers:
        # the other three connect at once anyway, and its failure is reported, not raised.
        held = []

        async def hold(reader, writer) -> None:
            held.append(writer)

        silent = await asyncio.start_server(hold, "127.0.0.1", NOBODY)
        fleet.add("Ghost", f"127.0.0.1:{NOBODY}", None, (0, 0, 0))
        t0 = time.monotonic()
        connecting = asyncio.get_running_loop().create_task(fleet.connect_all())
        await asyncio.sleep(1.0)
        early = {p.name: p.connected for p in fleet}
        results = await connecting
        took = time.monotonic() - t0
        silent.close()
        check("connect_all connects everyone at once: the three robots are connected while the silent one is still trying",
              early == {"Blaze": True, "Comet": True, "Rex": True, "Ghost": False}, f"after 1 s: {early}")
        ghost = fleet.get("Ghost").status()
        check("connect_all tolerates a robot it can't reach, reporting what went wrong",
              results["Ghost"] and "Couldn't reach" in results["Ghost"] and not ghost["connected"] and ghost["problem"]
              and all(results[n] is None for n in ("Blaze", "Comet", "Rex")),
              f"{str(results['Ghost'])[:75]}… after {took:.1f} s")
        await fleet.remove("Ghost")
        check("remove() takes a player off the team list", fleet.names() == ["Blaze", "Comet", "Rex"])

        st = {s["name"]: s for s in fleet.statuses()}
        check("statuses() reports 3 connected players", len(st) == 3 and all(s["connected"] for s in st.values()))
        for p in fleet:
            s = st[p.name]
            check(f"statuses(): {p.name} is at its start position on the field",
                  s["field_mm"] == [round(p.start[0]), round(p.start[1])] and s["heading_deg"] == p.start[2]
                  and s["located_by"] == "its start position", f"{s['field_mm']}, heading {s['heading_deg']}")
        check("statuses(): each robot's own battery", [st[n]["battery_percent"] for n in ("Blaze", "Comet", "Rex")] == [87, 76, 93])

        # Labels: name on the team colour, lights to match.
        results = await fleet.label_all()
        check("label_all labels everyone", results == {"Blaze": None, "Comet": None, "Rex": None}, str(results))
        for p, port in zip(fleet, ports):
            s = await mock_state(port)
            rgb = list(TEAM_RGB[p.team])
            check(f"label_all: {p.name}'s screen shows its name on {p.team}, with all six lights {p.team}",
                  s["screen"]["lines"] == [p.name] and s["screen"]["background"] == rgb and s["screen"]["font"] == "mono60"
                  and len(s["lights"]) == 6 and all(v == rgb for v in s["lights"].values()), json.dumps(s["screen"]))

        # Or with screen.py's player card (a face too), passed in so fleet.py doesn't depend on it.
        results = await fleet.label_all(card=screen.show_player_card)
        cards = [await mock_state(port) for port in ports]
        check("label_all(card=screen.show_player_card) draws each player's card, lights to match",
              all(r is None for r in results.values())
              and all(s["screen"]["lines"] == [p.name] and s["screen"]["background"] == list(TEAM_RGB[p.team])
                      and set(map(tuple, s["lights"].values())) == {TEAM_RGB[p.team]} for p, s in zip(fleet, cards)),
              json.dumps(cards[2]["screen"]))

        # Move Blaze; its field position follows, and the others identify it.
        blaze.robot.motion_enabled = True
        outcome = await blaze.robot.move(math.hypot(200, 600), math.degrees(math.atan2(200, 600)), 200)
        await blaze.robot.fresh_status()
        truth = await mock_state(PORT)
        pose = blaze.field_pose()
        check("after a move, Blaze's field position follows it", outcome == "completed"
              and dist(pose, (truth["x"], truth["y"])) < 5 and abs(pose[2] - truth["heading"]) < 1,
              f"fleet says ({pose[0]:.0f}, {pose[1]:.0f}), really ({truth['x']:.0f}, {truth['y']:.0f})")
        check("statuses() shows Blaze's new position", fleet.get("Blaze").status()["field_mm"] == [0, -100])
        await asyncio.sleep(0.3)  # everyone's next status shows Blaze where it is now

        seen = fleet.robots_in_view(comet)
        mine = [s for s in seen if s.player is blaze]
        check("Comet sees Blaze and knows it's a teammate", len(mine) == 1 and mine[0].relation == "teammate",
              json.dumps([s.as_dict() for s in seen]))
        check("…where Comet's camera puts Blaze is close to where Blaze is", mine and mine[0].off_by_mm < 100
              and dist(mine[0].field_xy, (truth["x"], truth["y"])) < 100,
              json.dumps(mine[0].as_dict()) if mine else "")
        det = next(d for d in comet.robot.detections() if d.name == "Robot" and abs(d.bearing - mine[0].bearing_deg) < 0.01)
        one = fleet.identify("Comet", det)
        check("identify() on a single detection agrees", one.player is blaze and one.relation == "teammate")
        seen = fleet.robots_in_view(rex)
        check("Rex (blue) sees both orange players as opponents, each once",
              sorted((s.player.name, s.relation) for s in seen if s.player) == [("Blaze", "opponent"), ("Comet", "opponent")]
              and len(seen) == 2, json.dumps([s.as_dict() for s in seen]))
        # A robot cut off at the picture's edge has no distance: matched by direction if only one player fits.
        cut = Detection("ai_object", "Robot", 3, round(vision_x_for_bearing(-20, 30)) - 20, 0, 40, 60)
        check("a cut-off robot is matched by direction when only one player is that way",
              fleet.identify(comet, cut).player is blaze and fleet.identify(comet, cut).distance_mm is None)
        cut = Detection("ai_object", "Robot", 3, round(vision_x_for_bearing(-13, 30)) - 20, 0, 40, 60)
        check("…and left unknown when two players are that way", fleet.identify(comet, cut).player is None)
        blaze.place(-450, 600, 0)  # deliberately wrong
        check("place(): put somewhere else, Blaze no longer matches what Comet sees",
              blaze.located_by == "placed by hand" and not any(s.player is blaze for s in fleet.robots_in_view(comet)))
        blaze.place(truth["x"], truth["y"], truth["heading"])
        check("place(): put back, it matches again", any(s.player is blaze for s in fleet.robots_in_view(comet)))

        # A dropped connection: carry on from where it was.
        before = blaze.field_pose()
        await blaze.disconnect()
        s = blaze.status()
        check("disconnected, Blaze shows where it was last seen", not s["connected"] and s["field_mm"] == [round(before[0]), round(before[1])])
        results = await fleet.connect_all()
        after = blaze.field_pose()
        check("reconnected, Blaze carries on from where it was", results["Blaze"] is None and dist(before, after) < 2
              and blaze.located_by == "where it was before it reconnected", f"{blaze.robot.connection_count} connections")

        # The ball: Comet drives into it, holds it, turns and kicks; the others watch it roll.
        comet.robot.motion_enabled = True
        await comet.robot.move(520, 0, 200)
        await asyncio.sleep(0.3)
        states = [await mock_state(p) for p in ports]
        check("Comet drove into the ball and holds it", states[1]["holding_ball"] and states[1]["ball"]["held_by"] == 2)
        check("only one robot holds the ball", [s["holding_ball"] for s in states] == [False, True, False])
        check("Comet's camera sees the ball in its kicker", held_object(comet.robot.detections()) == "SportsBall"
              and comet.status()["holding"] == "SportsBall")
        await comet.robot.turn(-19, 90)
        await comet.robot.kick("soft")
        s = await mock_state(PORT + 1)
        check("after the kick the ball is free and rolling", s["ball"]["held_by"] is None and s["ball"]["speed"] > 0
              and not s["holding_ball"], json.dumps(s["ball"]))
        watched: dict[str, list] = {"Blaze": [], "Rex": []}
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            await asyncio.sleep(0.1)
            for p in (blaze, rex):
                for d in p.robot.detections():
                    if d.name == "SportsBall" and not is_held(d) and (xy := p.locate(d)):
                        watched[p.name].append(xy)
        s = await mock_state(PORT + 1)
        for name, xys in watched.items():
            moved = dist(xys[0], xys[-1]) if xys else 0
            check(f"{name} saw the ball roll", len(xys) >= 3 and moved > 100,
                  f"{len(xys)} sightings, moved {moved:.0f} mm, last seen at ({xys[-1][0]:.0f}, {xys[-1][1]:.0f})" if xys else "")
        check("…and where it stopped matches the simulator", watched["Rex"] and dist(watched["Rex"][-1], (s["ball"]["x"], s["ball"]["y"])) < 80,
              f"really at ({s['ball']['x']:.0f}, {s['ball']['y']:.0f})")
        check("nobody holds the ball once it stops", s["ball"]["held_by"] is None and s["ball"]["speed"] == 0)

        # A robot that isn't on the team list.
        await fleet.remove("Rex")
        check("Rex is off the team list and disconnected", fleet.names() == ["Blaze", "Comet"] and not rex.connected)
        await asyncio.sleep(0.3)
        truth = await mock_state(PORT + 2)
        seen = fleet.robots_in_view(blaze)
        check("Blaze sees a robot that isn't on the team list: unknown (probably an opponent's)",
              len(seen) == 1 and seen[0].player is None and seen[0].relation == "unknown", json.dumps([x.as_dict() for x in seen]))
        check("…with an estimate of where it is on the field", seen and seen[0].field_xy
              and dist(seen[0].field_xy, (truth["x"], truth["y"])) < 100)
        seen = fleet.robots_in_view(comet)
        check("Comet sees it as unknown too", [x.relation for x in seen] == ["unknown"], json.dumps([x.as_dict() for x in seen]))

        # Back on the team list, connected before anyone says where it stands; then its start is set.
        rex = fleet.add("Rex", f"127.0.0.1:{PORT + 2}", "blue")
        await fleet.connect_all()
        s = rex.status()
        check("a player connected without a start position is connected but not on the field",
              s["connected"] and s["field_mm"] is None and "start position isn't set" in s["problem"])
        rex.start = (0, 700, 180)
        s = rex.status()
        check("setting its start position afterwards puts it on the field", s["field_mm"] == [0, 700]
              and s["heading_deg"] == 180 and s["located_by"] == "its start position" and "problem" not in s)
        await asyncio.sleep(0.3)
        check("…and Blaze recognises it as an opponent again",
              [(x.player.name if x.player else None, x.relation) for x in fleet.robots_in_view(blaze)] == [("Rex", "opponent")])

        results = await fleet.stop_all()
        check("stop_all stops every robot", results == {"Blaze": None, "Comet": None, "Rex": None})
        results = fleet.place_at_starts()
        check("place_at_starts() puts everyone back on their start spots",
              all(r is None for r in results.values())
              and all(dist(p.field_pose(), p.start) < 0.01 and abs((p.field_pose()[2] - p.start[2] + 180) % 360 - 180) < 0.01
                      for p in fleet)
              and {p.located_by for p in fleet} == {"back at its start position"})
    finally:
        results = await fleet.disconnect_all()
        check("disconnect_all disconnects everyone", all(r is None for r in results.values())
              and not any(p.connected for p in fleet))
        proc.terminate()
        proc.wait(timeout=5)
        log.close()


async def main() -> None:
    await roster_checks()
    await arena_checks()
    await end_to_end()
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
