"""Control panel for the VEX AIM robot, in a web browser on this computer (127.0.0.1 only).

It shows the live camera with detection boxes, and lets the person pick a team, label objects,
point things out to the AI assistant, teach the robot's onboard AI a colour, lock/unlock motion and
press STOP. Telemetry, a top-down map and a log of what the person, the assistant and the robot are
doing update several times a second.

The MCP server runs it in-process (the control_panel tool), sharing the robot connection and
the PanelState, so the assistant sees the team, labels and selections. It also runs on its own:

    vex-aim-panel --host 192.168.1.50
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import secrets
import statistics
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from . import network
from . import plays
from .fleet import Fleet
from . import routines
from . import screen
from . import strategy
from .aim_client import FLAG_CRASHED, AimError, AimRobot, held_object, is_held, vision_x_for_bearing
from .vision import Box, YoloDetector, fmt_bearing
from .world import (FIELDS, FOV_DEG, LOCATE_MAX_MM, MAP_CONFIRM, MAP_MERGE_MM, MOVERS, OBSTACLE_MM, TAG_ROLES, UNIQUE_OBJECTS,
                   PanelState, approx_distance_mm, calibration_key, cut_off, teach_colour, wrap180)

log = logging.getLogger("vex_aim.panel")

DEFAULT_YOLO_MODEL = "yolo26n.pt"  # Ultralytics downloads it on first use, into the data folder (paths.py)

# the bearing ruler drawn along the top of the picture: [degrees, x in the 640-wide video frame]
RULER_JSON = json.dumps([[b, round(vision_x_for_bearing(b) * 2, 1)] for b in range(-30, 31, 10)])


class ControlPanel:
    def __init__(self, robot: AimRobot, state: PanelState, port: int = 8765,
                 yolo: YoloDetector | None = None) -> None:
        self.robot = robot
        self.state = state
        self.port = port
        self.yolo = yolo
        self.use_yolo = False
        self.token = secrets.token_hex(16)  # only pages served from here can send commands
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._watcher: asyncio.Task | None = None
        self._fps = 0.0
        self._yolo_boxes: list[Box] = []
        self._trail: deque[tuple[float, float]] = deque(maxlen=400)
        self._last_connect_try = 0.0
        self._map_connection = None
        self._drive = {"target": (0.0, 0.0, 0.0), "until": 0.0, "sent": (0.0, 0.0, 0.0)}  # driver control
        self._match_task: asyncio.Task | None = None
        self._released = False  # let go of the robot on purpose, so its own menus work (e.g. to link a controller)
        self._looking_for: set[str] = set()  # players whose robots just switched networks, being looked for
        self._follow_task: asyncio.Task | None = None
        # The team list. This panel's robot is always on it, and is the one shown and driven ("selected").
        self.fleet = Fleet.from_dict(state.roster)
        self.on_select = None  # called with the newly selected robot, so the MCP server follows the panel
        me = next((p for p in self.fleet if p.host == robot.host), None)
        if me is None:
            names = {p.name.casefold() for p in self.fleet}
            name = state.player or next(f"AIM {i}" for i in range(1, 99) if f"aim {i}" not in names)
            me = self.fleet.add(name if name.casefold() not in names else f"{name} 2", robot.host, state.team, robot=robot)
        else:
            me._robot = robot  # share this connection rather than open a second one to the same robot
        self.fleet.select(me.name)
        self._sync_player()

    def _sync_player(self) -> None:
        """The selected player's name and team are what the rest of the panel (and the AI assistant) call
        "the player" and "the team"; and the team list is saved with the set-up."""
        me = self.fleet.pick()
        self.state.player, self.state.team = me.name, me.team
        self.state.roster = self.fleet.to_dict()
        self.state.save_setup()

    async def _select(self, name: str) -> None:
        """Show and drive another player's robot from this panel (and the assistant's tools, when in-process)."""
        player = self.fleet.select(name)
        await player.robot.ensure_connected()
        self.robot = player.robot
        self._trail.clear()
        self._map_connection = None  # a new robot: a new map frame
        self._drive["target"] = (0.0, 0.0, 0.0)
        self._sync_player()
        if self.on_select:
            self.on_select(player.robot)

    def _maybe_connect(self) -> None:
        """While the page is open, keep trying to reach the robot (e.g. once it wakes up)."""
        if self._released or self.robot.connected or time.monotonic() - self._last_connect_try < 5:
            return
        self._last_connect_try = time.monotonic()

        async def attempt() -> None:
            with contextlib.suppress(AimError):
                await self.robot.ensure_connected()

        asyncio.get_running_loop().create_task(attempt())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self, use_yolo: bool = True) -> str:
        self.use_yolo = use_yolo and self.yolo is not None
        if self.use_yolo:
            self.yolo.warm_up()  # ~10 s in the background; frames show without YOLO until then
        if self._server is None:
            try:
                self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
            except OSError as e:
                raise AimError(f"Couldn't open the control panel on port {self.port} ({e}). "
                               "Is another one already running?") from e
            self._watcher = asyncio.get_running_loop().create_task(self._watch())
            self._driver = asyncio.get_running_loop().create_task(self._drive_loop())
            self.state.log("robot", f"control panel opened at {self.url}")
        return self.url

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for w in list(self._writers):
            w.close()
        if self._watcher:
            self._watcher.cancel()
            self._driver.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)
        self._server = None

    # ----------------------------------------------------------------------------------
    # Watching the robot: trail for the map, and log-worthy changes
    # ----------------------------------------------------------------------------------

    async def _watch(self) -> None:
        was_connected, crashed, holding = None, False, None
        while True:
            await asyncio.sleep(0.25)
            connected = self.robot.connected
            if connected != was_connected and was_connected is not None:
                self.state.log("robot", "connected" if connected else self.robot.connection_problem())
            was_connected = connected
            if not connected:
                continue
            with contextlib.suppress(AimError, KeyError, ValueError):
                self.state.note_battery(self.robot.snapshot()["battery_percent"])
                self._update_map()
                x, y = self.robot.position
                if not self._trail or math.dist(self._trail[-1], (x, y)) > 10:
                    self._trail.append((x, y))
                now_crashed = bool(self.robot.flags & FLAG_CRASHED)
                if now_crashed and not crashed:
                    self.state.log("robot", "bump detected")
                crashed = now_crashed
                now_holding = next((d.name for d in self.robot.detections() if is_held(d)), None)
                if now_holding != holding:
                    self.state.log("robot", f"holding {now_holding}" if now_holding else f"no longer sees {holding} in the kicker")
                holding = now_holding

    async def _drive_loop(self) -> None:
        """Driver control: pass the latest stick position to the wheels, and stop the moment the page
        stops sending (the driver let go, the tab closed or the connection dropped)."""
        while True:
            await asyncio.sleep(0.05)
            d = self._drive
            target = d["target"] if time.monotonic() < d["until"] and self.state.mode == "driver" else (0.0, 0.0, 0.0)
            if not any(target) and self.state.mode == "driver" and self.robot.motion_enabled:
                target = self._aim_stick()  # the robot's own AIM controller, if one is paired and pushed
            if target == d["sent"]:
                continue
            with contextlib.suppress(AimError):
                if any(target):
                    await self.robot.drive_vector(*target)
                else:
                    await self.robot.stop()
                d["sent"] = target

    def _aim_controller(self) -> dict:
        """The AIM controller paired with the robot, as its status reports it (sticks, buttons, battery)."""
        with contextlib.suppress(AimError, KeyError, TypeError, ValueError):
            c = self.robot.status["controller"]
            x, y, flags, battery = int(c.get("stick_x", 0)), int(c.get("stick_y", 0)), int(str(c.get("flags", "0")), 16), int(c.get("battery", 0))
            return {"connected": bool(battery or flags or x or y), "x": x, "y": y, "flags": f"0x{flags:04x}", "battery": battery}
        return {"connected": False}

    def _aim_stick(self) -> tuple[float, float, float]:
        """Arcade-style driving from the AIM controller's one stick: up/down drives, left/right turns."""
        c = self._aim_controller()
        if not c["connected"]:
            return (0.0, 0.0, 0.0)
        scale = routines.MAX_SPEED_PERCENT / 100
        forwards, turn = (v if abs(v) > 10 else 0 for v in (c["y"], c["x"]))  # a small dead zone around the middle
        return (round(max(-100, min(100, forwards)) * scale, 1), 0.0, round(max(-100, min(100, turn)) * scale * 0.7, 1))

    async def _run_match(self, auto_s: float, driver_s: float) -> None:
        """A match: an autonomous period (routines, plays, the assistant), then driver control, then everything stops."""
        state = self.state
        try:
            state.mode = "auto"
            state.match = {"phase": "auto", "ends": time.time() + auto_s, "auto_s": auto_s, "driver_s": driver_s}
            state.log("robot", f"Match started: {auto_s:g} s of autonomous")
            await asyncio.sleep(auto_s)
            routines.cancel(state)
            with contextlib.suppress(AimError):
                await self.robot.stop()
            state.mode = "driver"
            state.match.update(phase="driver", ends=time.time() + driver_s)
            state.log("robot", f"Driver control for {driver_s:g} s!")
            await asyncio.sleep(driver_s)
            state.log("robot", "Match over")
        finally:
            self._drive["target"] = (0.0, 0.0, 0.0)
            state.match, state.mode = None, "auto"
            with contextlib.suppress(AimError):
                await self.robot.stop()

    def _update_map(self) -> None:
        """Remember where each object with a distance estimate is, in the robot's odometry frame
        (mm from where it connected). Repeat sightings refine the position; objects stay on the
        map after they leave the camera's view."""
        state = self.state
        if self.robot.connection_count != self._map_connection:  # a new connection starts a new frame
            state.map.clear()
            self._trail.clear()
            self._map_connection = self.robot.connection_count
            state.field.update(offset=[0.0, 0.0], located_by=None)
        x, y = self.robot.position
        heading, now = self.robot.heading, time.monotonic()
        fixes = []  # where pinned markers in view say the odometry frame sits on the field
        for d in self.robot.detections():
            if is_held(d) or cut_off(d):  # held objects move with the robot; cut-off ones look too far away
                continue
            ckey = calibration_key(d)
            state.smallest[ckey] = min(state.smallest.get(ckey, 10_000), d.width)
            mm = approx_distance_mm(d, state)
            if not mm:
                continue
            a = math.radians(heading + d.bearing)
            ox, oy = x + mm * math.sin(a), y + mm * math.cos(a)
            tag = state.tags.get(d.id) if d.kind == "apriltag" else None
            if tag and tag.get("pin") and state.field["name"] in FIELDS:
                if mm <= LOCATE_MAX_MM:
                    fixes.append((tag["pin"][0] - ox, tag["pin"][1] - oy, state.tag_name(d.id)))
                continue  # a pinned marker stays where it was pinned
            key = [d.kind, d.name, d.id]
            near = min((o for o in state.map if o["key"] == key),
                       key=lambda o: math.dist((o["x"], o["y"]), (ox, oy)), default=None)
            moving = d.name in MOVERS  # a moving robot travels further between sightings than a still one jitters
            close = near and math.dist((near["x"], near["y"]), (ox, oy)) < max(MAP_MERGE_MM, 0.25 * mm, 300 if moving else 0)
            if near and moving:  # how fast it's going: a best-fit line through the last second or so of sightings
                hist = [h for h in near.get("hist", []) if now - h[0] < 1.5] + [(now, ox, oy)]
                near["hist"] = hist[-10:]
                if len(hist) >= 4 and hist[-1][0] - hist[0][0] >= 0.6:
                    mt = statistics.fmean(h[0] for h in hist)
                    var = sum((h[0] - mt) ** 2 for h in hist)
                    near["vx"], near["vy"] = (sum((h[0] - mt) * (h[i] - statistics.fmean(g[i] for g in hist)) for h in hist) / var
                                              for i in (1, 2))
                else:
                    near["vx"] = near["vy"] = 0.0
            if close:
                k = 0.6 if moving else 0.3  # follow movers closely; smooth still things
                near["x"] += k * (ox - near["x"])
                near["y"] += k * (oy - near["y"])
                near.update(seen=now, display=self.state.display_name(d, heading), sightings=near["sightings"] + 1)
            elif near and (d.kind == "apriltag" or d.name in UNIQUE_OBJECTS):  # there's only one: it has moved
                near.update(x=ox, y=oy, seen=now, display=self.state.display_name(d, heading))
            elif len(state.map) < 150:
                state.map.append({"key": key, "kind": d.kind, "name": d.name, "id": d.id, "x": ox, "y": oy,
                                  "seen": now, "display": state.display_name(d, heading), "sightings": 1})
        if fixes:  # glide towards what the markers say (sizes, and so distances, are noisy)
            off = state.field["offset"]
            off[0] += 0.3 * (statistics.fmean(f[0] for f in fixes) - off[0])
            off[1] += 0.3 * (statistics.fmean(f[1] for f in fixes) - off[1])
            names = " and ".join(f[2] for f in fixes)
            if not (state.field["located_by"] or "").startswith("AprilTag"):
                state.log("robot", f"worked out where it is on the field from {names}")
            state.field["located_by"] = names

    # ----------------------------------------------------------------------------------
    # HTTP
    # ----------------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            request = (await asyncio.wait_for(reader.readline(), timeout=10)).decode("latin-1").split()
            headers = {}
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            method, path = (request[0], request[1]) if len(request) >= 2 else ("GET", "/")
            if method == "POST" and path.startswith("/api/"):
                length = min(int(headers.get("content-length", "0") or 0), 65536)
                body = await reader.readexactly(length) if length else b""
                status, reply = await self._api(path, headers, body)
                await self._send(writer, status, "application/json", json.dumps(reply).encode())
            elif path.startswith("/stream.mjpg"):
                await self._stream(writer)
            elif path.startswith("/state.json"):
                since = int(path.partition("since=")[2] or 0) if "since=" in path else 0
                if self.robot.connected:  # an open panel is using the robot, even without the video
                    self.robot.last_activity = time.monotonic()
                await self._send(writer, "200 OK", "application/json", json.dumps(self._state_json(since)).encode())
            elif path.startswith("/debug/status.json"):  # the robot's latest raw status, for curious people
                raw = self.robot.status if self.robot.connected else {"connected": False}
                await self._send(writer, "200 OK", "application/json", json.dumps(raw, indent=1).encode())
            elif path == "/" or path.startswith("/?"):
                page = (Path(__file__).with_name("panel.html")).read_text().replace("__PANEL_TOKEN__", self.token)
                page = page.replace("__RULER__", RULER_JSON).replace("__FIELDS__", json.dumps(FIELDS))
                await self._send(writer, "200 OK", "text/html; charset=utf-8", page.encode())
            else:
                await self._send(writer, "404 Not Found", "text/plain", b"not found")
        except (ConnectionError, TimeoutError, AimError, asyncio.IncompleteReadError) as e:
            log.info("panel client finished: %s", e)
        except Exception:
            log.exception("panel request failed")
        finally:
            self._writers.discard(writer)
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, status: str, content_type: str, body: bytes) -> None:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _api(self, path: str, headers: dict, body: bytes) -> tuple[str, dict]:
        origin = headers.get("origin")
        if headers.get("x-panel-token") != self.token or (
                origin and origin not in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")):
            return "403 Forbidden", {"error": "Commands are only accepted from the control panel page."}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return "400 Bad Request", {"error": "Expected JSON."}
        try:
            return "200 OK", await self._do(path.removeprefix("/api/"), data)
        except AimError as e:
            return "409 Conflict", {"error": str(e)}
        except (KeyError, TypeError, ValueError) as e:
            return "400 Bad Request", {"error": f"Bad request: {e}"}

    async def _do(self, action: str, data: dict) -> dict:
        robot, state = self.robot, self.state
        if action == "stop":
            stopped = routines.cancel(state)  # whatever the robot is doing by itself, whoever started it
            self._drive["target"] = (0.0, 0.0, 0.0)
            await robot.ensure_connected()
            await robot.stop()
            state.log("you", "pressed STOP" + (" (ending what the robot was doing by itself)" if stopped else ""))
            return {"ok": True}
        if action == "team":
            colour = data.get("colour")
            if colour not in ("blue", "orange", None):
                raise ValueError("colour must be blue, orange or null")
            self.fleet.pick().team = colour
            self._sync_player()
            state.action(f"chose the {colour} team" if colour else "cleared the team", kind="team", team=colour)
            if state.player and robot.connected:
                with contextlib.suppress(AimError):
                    await self._label_robot()
            return {"team": colour}
        if action == "player":
            name = " ".join(str(data["name"]).split())[:16]
            if not name:
                raise ValueError("give the player a name")
            if name != self.fleet.pick().name:
                self.fleet.rename(self.fleet.pick().name, name)
            self._sync_player()
            state.action(f"named the robot “{name}”" if name else "cleared the robot's player name", kind="player", name=name)
            await robot.ensure_connected()
            await self._label_robot()
            return {"player": state.player}
        if action == "react":
            expression = str(data.get("expression", "happy"))
            await robot.ensure_connected()
            await screen.react(robot, state, expression, data.get("caption"))
            state.log("you", f"showed {'an' if expression[0] in 'aeiou' else 'a'} {expression} face on the robot")
            return {"expression": expression}
        if action == "player_add":
            player = self.fleet.add(str(data["name"]), str(data["host"]), data.get("team") or None)
            self._sync_player()
            state.action(f"added {player.name} ({player.host}) to the team list", kind="player_add", name=player.name)
            with contextlib.suppress(AimError):
                await player.connect()
            return {"players": self.fleet.statuses()}
        if action == "player_pair":  # a robot found on the network joins the team: on the list, connected, labelled
            host = str(data["host"]).strip()
            taken = {p.name.casefold() for p in self.fleet}
            name = " ".join(str(data.get("name") or "").split()) or next(f"AIM {i}" for i in range(2, 99) if f"aim {i}" not in taken)
            team = data["team"] if "team" in data else state.team
            player = self.fleet.add(name, host, team or None)
            self._sync_player()
            state.action(f"paired {player.name} ({host}) with the {team or 'no'} team", kind="player_add", name=player.name)
            try:
                await player.connect()  # it says hello on its screen, then shows its usual card
                await self._note_macs([player.host])
                await screen.react(player.robot, SimpleNamespace(player=player.name, team=player.team), "excited", "On the team!")
            except AimError as e:
                return {"players": self.fleet.statuses(), "warning": f"{player.name} is on the list, but: {e}"}
            return {"players": self.fleet.statuses(), "name": player.name}
        if action == "player_remove":
            player = self.fleet.get(str(data["name"]))
            if player.robot is robot:
                raise AimError("That's the robot this panel is showing: show another one first.")
            await self.fleet.remove(player.name)
            self._sync_player()
            state.log("you", f"took {player.name} off the team list")
            return {"players": self.fleet.statuses()}
        if action == "player_team":
            self.fleet.get(str(data["name"])).team = data.get("team") or None
            self._sync_player()
            return {"players": self.fleet.statuses()}
        if action == "player_select":
            await self._select(str(data["name"]))
            state.log("you", f"now showing {self.fleet.pick().name}'s robot ({robot.host} → {self.robot.host})")
            return {"selected": self.fleet.selected}
        if action == "players_connect":
            results = await self.fleet.connect_all()
            state.log("you", "connected the team: " + ", ".join(f"{n} {'✓' if not e else '✗'}" for n, e in results.items()))
            return {"results": results}
        if action == "players_label":
            results = await self.fleet.label_all(card=screen.show_player_card)
            state.log("you", "showed every player's card on its robot")
            return {"results": results}
        if action == "play":  # soccer plays (plays.py): the robot does them by itself; STOP ends them
            which, speed = str(data["play"]), float(data.get("speed_percent", 40))
            jobs = {"fetch": ("fetching the ball", lambda: plays.fetch_ball(robot, state, speed)),
                    "score": ("scoring", lambda: plays.shoot(robot, state, speed)),
                    "guard": ("guarding the goal", lambda: plays.guard_goal(robot, state, float(data.get("seconds", 30)), None, speed)),
                    "go_to": ("going to a spot", lambda: plays.go_to(robot, state, float(data["x"]), float(data["y"]), speed)),
                    "pass_to": ("passing", lambda: plays.pass_to(robot, state, float(data["x"]), float(data["y"]), speed))}
            if which not in jobs:
                raise ValueError(f"unknown play {which!r}")
            name, make = jobs[which]
            return await self._start_routine(name, make())
        if action == "net_info":  # the robot's network, from its setup page (never its password), and this computer's
            info = {"host": robot.host, "computer": await asyncio.to_thread(network.this_computer_network), "venues": state.venues,
                    "known": await asyncio.to_thread(network.known_networks), "team": [p.name for p in self.fleet]}
            try:
                info["robot"] = await network.robot_network(robot.host)
            except (OSError, asyncio.TimeoutError) as e:
                info["robot_problem"] = (f"It's switching to its own hotspot: join its AIM-… Wi-Fi on this Mac (the password is on its screen)."
                                         if robot.host == network.AP_HOST else f"Couldn't reach it ({e or 'no answer'}).")
                with contextlib.suppress(OSError, asyncio.TimeoutError):  # is this Mac on some robot's own hotspot?
                    if robot.host != network.AP_HOST:
                        info["hotspot"] = await network.robot_network(network.AP_HOST, timeout=1)
            return info
        if action == "net_scan":  # the Wi-Fi networks near this Mac (the first time, macOS asks to allow the scan helper)
            result = await network.scan_wifi(check_only=bool(data.get("check")))
            if result.get("networks") is not None:
                state.log("you", f"scanned for Wi-Fi networks: {len(result['networks'])} nearby")
            return result
        if action == "net_find":
            robots = await network.find_robots()
            moved = await self._match_found(robots)
            state.log("you", f"looked for robots on this network: found {len(robots)}" + (f" ({'; '.join(moved)})" if moved else ""))
            return {"robots": [{**r, "this": r["host"] == self.robot.host} for r in robots], "moved": moved}
        if action == "net_use":  # show the robot at this address: a player's, or the panel's own robot somewhere new (e.g. its hotspot)
            host = str(data["host"]).strip()
            player = next((p for p in self.fleet if p.host == host), None)
            if player:
                await self._select(player.name)
            else:
                await self._readdress(self.fleet.pick(), host, wait=True)
            state.log("you", f"switched the panel to the robot at {host}")
            return {"host": self.robot.host}
        if action == "net_blink":  # which robot is that? It flashes white and rings
            host = str(data["host"]).strip()
            player = next((p for p in self.fleet if p.host == host), None)
            other = player.robot if player else AimRobot(host)
            try:
                await other.ensure_connected()
                for rgb in [(255, 255, 255), (0, 0, 0)] * 3:
                    await other.set_led("all", rgb)
                    await asyncio.sleep(0.2)
                await other.play_sound("doorbell", 60)
                if player:
                    await other.set_led("all", screen.TEAM_RGB.get(player.team, screen.TEAM_RGB[None]))
            finally:
                if player is None:
                    await other.disconnect()
            return {"host": host}
        if action == "net_remember":  # remember a network to send robots to later; its password goes in this Mac's keychain
            if data.get("network"):  # picked from a scan or the networks this Mac has joined, or typed
                name, password = str(data["network"]).strip(), str(data.get("password") or "")
                if data.get("from_mac"):  # the password this Mac saved for it: macOS asks the person first, in its own dialog
                    network.check_details(name, "")
                    password = await asyncio.to_thread(network.mac_saved_password, name)
                    if password is None:
                        raise AimError(f"This Mac didn't hand over the password for “{name}” (cancelled, or none saved): type it in instead.")
                network.check_details(name, password)
                await asyncio.to_thread(network.save_venue_password, name, password)
            else:  # the robot's own network: the password comes from the robot, unseen
                try:
                    name = await network.remember_robot_network(robot.host)
                except (OSError, asyncio.TimeoutError) as e:
                    raise AimError(f"Couldn't read the robot's network: {e or 'it did not answer'}.") from e
            if name not in state.venues:
                state.venues.append(name)
                state.save_setup()
            state.log("you", f"remembered the Wi-Fi network “{name}” (its password is in this Mac's keychain)")
            return {"venues": state.venues, "network": name}
        if action == "net_move":  # send this robot, the whole team, or one on its own hotspot to another network
            name, target = str(data["network"]).strip(), str(data.get("target") or "this")
            password = str(data.get("password") or "")
            if not password and name in state.venues:
                password = await asyncio.to_thread(network.venue_password, name) or ""
            network.check_details(name, password)
            hosts = ([p.host for p in self.fleet if p.host != robot.host] + [robot.host] if target == "team"  # the panel's own last
                     else [str(data.get("host") or robot.host).strip()])
            if data.get("remember"):
                await asyncio.to_thread(network.save_venue_password, name, password)
                if name not in state.venues:
                    state.venues.append(name)
                    state.save_setup()
            results: dict[str, str | None] = {}
            for host in hosts:
                try:
                    await network.send_to_network(host, name, password)
                    results[host] = None
                except (OSError, asyncio.TimeoutError) as e:
                    results[host] = str(e) or "it didn't answer"
            moved = [h for h, e in results.items() if e is None]
            if not moved:
                raise AimError(f"Couldn't reach {'the robot' if len(hosts) == 1 else 'any of the robots'}: {results[hosts[0]]}")
            await self._note_macs(moved)
            self._follow([p.name for p in self.fleet if p.host in moved])
            state.action(f"moved {', '.join(moved)} to the Wi-Fi network “{name}”", kind="network", network=name, hosts=moved)
            text = (f"Told the robot at {moved[0]} to join “{name}”." if len(hosts) == 1
                    else f"Told {len(moved)} of {len(hosts)} robots to join “{name}”.")
            return {"ok": len(moved) == len(hosts), "text": text, "network": name, "results": results}
        if action == "net_hotspot":  # the robot starts its own Wi-Fi, for this Mac to join directly: no router needed
            host, name = robot.host, None
            with contextlib.suppress(OSError, asyncio.TimeoutError, ValueError):  # remember its network first, to switch back to
                name = await network.remember_robot_network(host)
                if name not in state.venues:
                    state.venues.append(name)
                    state.save_setup()
            try:
                new_host = await network.send_to_hotspot(host)
            except (OSError, asyncio.TimeoutError) as e:
                raise AimError(f"Couldn't reach the robot: {e or 'it did not answer'}.") from e
            await self._note_macs([host])
            await self._readdress(self.fleet.pick(), new_host)
            state.action(f"switched the robot at {host} to its own hotspot", kind="network", network=None, hosts=[host])
            return {"ok": True, "host": new_host, "remembered": name}
        if action == "net_forget":
            name = str(data["network"])
            if name in state.venues:
                state.venues.remove(name)
                state.save_setup()
            await asyncio.to_thread(network.forget_venue_password, name)
            state.log("you", f"forgot the venue network “{name}”")
            return {"venues": state.venues}
        if action == "release":  # the robot's own menus only work with no program connected
            routines.cancel(state)
            self._drive["target"] = (0.0, 0.0, 0.0)
            self._released = True
            await robot.disconnect()
            state.log("you", "let go of the robot, so its own Drive mode and menus work (e.g. to drive with the AIM controller)")
            return {"released": True}
        if action == "resume":
            self._released = False
            await robot.ensure_connected()
            state.log("you", "reconnected to the robot")
            return {"released": False}
        if action == "mode":
            mode = str(data["mode"])
            if mode not in ("auto", "driver"):
                raise ValueError("mode must be auto or driver")
            if state.match:
                raise AimError("A match is on: it switches between autonomous and driver by itself.")
            if mode == "driver":
                routines.cancel(state)
            state.mode = mode
            self._drive["target"] = (0.0, 0.0, 0.0)
            state.action("took driver control" if mode == "driver" else "handed control back (autonomous)", kind="mode", mode=mode)
            return {"mode": mode}
        if action == "drive":
            if state.mode != "driver":
                raise AimError("Switch to Driver mode to drive from the panel.")
            if not robot.motion_enabled:
                raise AimError("Unlock motion first (top right).")
            scale = routines.MAX_SPEED_PERCENT
            self._drive["target"] = tuple(round(max(-1.0, min(1.0, float(data.get(k, 0)))) * scale, 1) for k in ("y", "x", "r"))
            self._drive["until"] = time.monotonic() + 0.35  # keeps going only while the page keeps sending
            return {"ok": True}
        if action == "driver_kick":
            if state.mode != "driver":
                raise AimError("Switch to Driver mode to kick from the panel.")
            await robot.ensure_connected()
            await robot.kick(str(data.get("strength", "medium")))
            state.log("you", f"kicked ({data.get('strength', 'medium')})")
            return {"ok": True}
        if action == "match":
            if data.get("action") == "stop":
                if self._match_task and not self._match_task.done():
                    self._match_task.cancel()
                    state.log("you", "stopped the match")
                return {"ok": True}
            await robot.ensure_connected()
            if not robot.motion_enabled:
                raise AimError("Unlock motion first: the robot moves in a match.")
            if self._match_task and not self._match_task.done():
                raise AimError("A match is already on.")
            auto_s, driver_s = float(data.get("auto_s", 15)), float(data.get("driver_s", 60))
            if not (0 <= auto_s <= 300 and 0 < driver_s <= 600):
                raise ValueError("autonomous can be 0-300 s and driver 1-600 s")
            self._match_task = asyncio.get_running_loop().create_task(self._run_match(auto_s, driver_s))
            await asyncio.sleep(0)
            return {"started": True}
        if action == "scan":
            return await self._start_routine("scan", self._scan(float(data.get("speed_percent", 30))))
        if action == "explore":
            return await self._start_routine("exploration", routines.explore_field(robot, state, float(data.get("speed_percent", 30))))
        if action == "kick_test":
            return await self._start_routine("kick test", routines.kick_test(
                robot, state, str(data["strength"]), bool(data.get("ball_confirmed"))))
        if action == "kick_record":
            routines.record_kick(state, str(data["strength"]), float(data["distance_cm"]) * 10)
            return {"kick_reach": state.kick_reach()}
        if action == "speed_test":
            return await self._start_routine("speed test", routines.speed_test(
                robot, state, float(data.get("speed_percent", 30)), float(data.get("distance_cm", 50)) * 10))
        if action == "forget_ability":
            kind, key = str(data["kind"]), str(data["key"])
            if state.abilities.get(kind, {}).pop(key, None) is not None:
                state.save_setup()
                state.log("you", f"cleared the {key}{'%' if kind == 'drive' else ''} {'speed' if kind == 'drive' else 'kick'} results")
            return {"ok": True}
        if action == "motion":
            enabled = bool(data["enabled"])
            await robot.ensure_connected()
            robot.motion_enabled = enabled
            state.action("unlocked motion (robot is on the floor with clear space)" if enabled else "locked motion",
                         kind="motion", enabled=enabled)
            return {"motion_enabled": enabled}
        if action == "label":
            obj, label = data["object"], str(data["label"]).strip()[:30]
            if obj["kind"] == "color":
                state.colours[int(obj["id"])]["label"] = label
            elif obj["kind"] == "apriltag":
                state.labels = [e for e in state.labels if e.get("tag") != obj["id"]]
                if label:
                    state.labels.append({"name": obj["name"], "label": label, "tag": obj["id"]})
            else:
                heading = float(obj["heading"])
                state.labels = [e for e in state.labels if not (
                    e["name"] == obj["name"] and e.get("heading") is not None and abs(wrap180(e["heading"] - heading)) < 10)]
                if label:
                    state.labels.append({"name": obj["name"], "label": label, "heading": round(heading, 1)})
            state.action(f"labelled the {obj['name']} as “{label}”" if label else f"removed the label from the {obj['name']}",
                         kind="label", object=obj, label=label)
            if obj["kind"] == "apriltag":
                state.save_setup()
            return {"ok": True}
        if action == "select":
            obj = data["object"]
            state.selected = obj
            state.action(f"pointed out {obj['display']} ({fmt_bearing(obj['bearing'])}, heading {obj['heading']:.0f}°)"
                         + (f": “{data['note']}”" if data.get("note") else ""), kind="select", object=obj, note=data.get("note"))
            return {"ok": True}
        if action == "clear_map":
            state.map.clear()
            self._trail.clear()
            state.log("you", "cleared the map")
            return {"ok": True}
        if action == "calibrate":
            obj, cm = data["object"], float(data["distance_cm"])
            if not 2 <= cm <= 1000:
                raise ValueError("the distance must be between 2 and 1000 cm")
            # the freshest, fully visible sighting of that object (closest bearing to where it was clicked)
            same = [d for d in robot.detections() if d.name == obj["name"] and d.kind == obj["kind"] and not cut_off(d)]
            d = min(same, key=lambda d: abs(d.bearing - float(obj["bearing"])), default=None)
            if d is None:
                raise AimError("Can't see that object fully right now: it may be cut off at the edge of the picture.")
            key = calibration_key(d)
            state.calibration.setdefault(key, []).append([d.width, cm * 10])
            state.save_setup()
            k = state.size_constant(key)
            state.log("you", f"measured the {state.display_name(d, robot.heading)} at {cm:g} cm ({d.width} px wide); "
                             f"its distance constant is now {k:.0f}")
            return {"key": key, "k": k, "samples": len(state.calibration[key])}
        if action == "forget_calibration":
            if state.calibration.pop(str(data["key"]), None) is not None:
                state.save_setup()
                state.log("you", f"cleared the distance measurements for {data['key']}")
            return {"ok": True}
        if action == "colour":
            taught = await teach_colour(robot, state, str(data["label"]), data["box"], data.get("tolerance", "normal"),
                                        data.get("width_mm"))
            state.action(f"taught the robot a colour: “{taught['label']}” (rgb {tuple(taught['rgb'])}, slot {taught['id']})",
                         kind="colour", **taught)
            return taught
        if action == "forget_colour":
            colour_id = int(data["id"])
            removed = state.colours.pop(colour_id, None)
            if removed:
                state.log("you", f"forgot the colour “{removed['label']}”")
            return {"ok": True}
        if action == "field":
            name = str(data["name"])
            if name != "fit" and name not in FIELDS:
                raise ValueError(f"unknown field {name!r}")
            state.field["name"] = name
            state.save_setup()
            state.log("you", f"showed the map as the {FIELDS[name]['name']}" if name in FIELDS else "showed the map fitted to what it's seen")
            return {"field": name}
        if action == "place_robot":
            await robot.ensure_connected()
            fx, fy = float(data["x"]), float(data["y"])
            rx, ry = robot.position
            state.field.update(offset=[fx - rx, fy - ry], located_by="placed by hand")
            with contextlib.suppress(AimError):  # the team list keeps every player's place on the field too
                self.fleet.pick().place(fx, fy, robot.heading)
            state.action(f"placed the robot on the field at x {fx / 10:.0f}, y {fy / 10:.0f} cm", kind="place", x=fx, y=fy)
            return {"offset": state.field["offset"]}
        if action == "tag":
            tag_id = int(data["id"])
            if not 0 <= tag_id <= 36:
                raise ValueError("AprilTag ids go from 0 to 36")
            tag = state.tags.setdefault(tag_id, {"role": None, "pin": None})
            tag.setdefault("note", None)
            if "label" in data:
                label = " ".join(str(data["label"] or "").split())[:30]
                state.labels = [e for e in state.labels if e.get("tag") != tag_id]
                if label:
                    state.labels.append({"name": f"AprilTag {tag_id}", "label": label, "tag": tag_id})
            if "note" in data:
                tag["note"] = " ".join(str(data["note"] or "").split())[:160] or None
                if tag["note"]:
                    state.action(f"said what {state.tag_name(tag_id)} means: “{tag['note']}”", kind="tag", id=tag_id)
            if "role" in data:
                if data["role"] not in (None, *TAG_ROLES):
                    raise ValueError("role must be marker, obstacle or null")
                tag["role"] = data["role"]
                state.action(f"made {state.tag_name(tag_id)} " + {"marker": "a field marker", "obstacle": "an obstacle to avoid",
                                                                   None: "just a tag"}[data["role"]], kind="tag", id=tag_id)
            if "pin" in data:
                tag["pin"] = [float(data["pin"][0]), float(data["pin"][1])] if data["pin"] else None
                if tag["pin"]:  # it lives at its pin now, not where sightings put it
                    state.map = [o for o in state.map if not (o["kind"] == "apriltag" and o["id"] == tag_id)]
                    tag["role"] = tag["role"] or "marker"
                state.action(f"pinned {state.tag_name(tag_id)} to the field at x {tag['pin'][0] / 10:.0f}, y {tag['pin'][1] / 10:.0f} cm"
                             if tag["pin"] else f"unpinned {state.tag_name(tag_id)}", kind="tag", id=tag_id)
            if tag == {"role": None, "pin": None, "note": None}:
                del state.tags[tag_id]
            state.save_setup()
            return {"id": tag_id, **state.tags.get(tag_id, {"role": None, "pin": None, "note": None})}
        if action == "apriltags":
            await robot.ensure_connected()
            await robot.set_detection(apriltags=bool(data["enabled"]))
            state.log("you", f"turned AprilTag detection {'on' if data['enabled'] else 'off'}")
            return {"apriltags": robot.apriltags_on}
        if action == "yolo":
            self.use_yolo = bool(data["enabled"]) and self.yolo is not None
            if self.use_yolo:
                self.yolo.warm_up()
            state.log("you", f"turned YOLO {'on' if self.use_yolo else 'off'}")
            return {"yolo": self.use_yolo}
        raise ValueError(f"unknown action {action!r}")

    async def _start_routine(self, name: str, job) -> dict:
        """Start something the robot does by itself, in the background; the page follows it in
        state.json's "routine", and STOP ends it."""
        try:
            await self.robot.ensure_connected()
            if not self.robot.motion_enabled:
                raise AimError("Unlock motion first: the robot has to move for this.")
            if self.state.mode == "driver":
                raise AimError("You're driving (Driver mode). Switch to Auto to let the robot do this by itself.")
            if busy := self.state.routine_status():
                raise AimError(f"The robot is busy ({busy['name']}). Wait for it, or press STOP.")
        except AimError:
            job.close()
            raise

        async def runner() -> None:
            try:
                result = await routines.run(self.state, name, job)
                if isinstance(result, str):
                    self.state.log("robot", result)
            except AimError as e:
                self.state.log("robot", f"{name} ended: {e}")

        asyncio.get_running_loop().create_task(runner())
        await asyncio.sleep(0)  # let it register, so the page sees it straight away
        return {"started": name}

    async def _scan(self, speed_percent: float) -> str:
        routines.progress(self.state, "scanning around the robot")
        await routines.scan_here(self.robot, self.state, routines.speeds(speed_percent)[1])
        return "Scanned a full circle. " + routines.map_summary(self.state)

    async def _readdress(self, player, host: str, wait: bool = False):
        """The same robot at a new address (it switched networks, or the router gave it another one).
        Returns its player: a new one, unless it's the panel's own robot, whose connection is shared with
        the assistant's tools and just reconnects (in the background, unless wait)."""
        if player is not self.fleet.pick():
            return await self.fleet.set_host(player.name, host)
        self.fleet._check_unique(player.name, host, ignore=player)
        player.host = host
        self._sync_player()
        if wait:
            await self.robot.connect(host)
        else:
            async def reconnect() -> None:
                with contextlib.suppress(AimError):
                    await self.robot.connect(host)
            asyncio.get_running_loop().create_task(reconnect())
        return player

    async def _note_macs(self, hosts: list[str]) -> None:
        """Note the hardware addresses of team robots at these addresses (this computer has just talked to them)."""
        macs = await asyncio.to_thread(network.mac_addresses)
        for p in self.fleet:
            if p.host in hosts and not p.mac:
                p.mac = network.mac_of(p.host, macs)
        self._sync_player()

    async def _match_found(self, robots: list[dict]) -> list[str]:
        """Match robots found on the network to the team list: by their radio's hardware address, else by address.
        A player whose robot turned up at a new address follows it there. Marks each robot with its player."""
        moved = []
        for r in robots:
            mac = r.get("mac")
            player = next((p for p in self.fleet if network.same_radio(p.mac, mac)), None)
            at_host = next((p for p in self.fleet if p.host == r["host"]), None)
            if player is None and at_host is not None and not (at_host.mac and mac):  # no hardware addresses to go by
                player = at_host
            if player is None:
                continue
            if player.host != r["host"]:
                try:
                    player = await self._readdress(player, r["host"])
                except AimError as e:
                    log.info("couldn't follow %s to %s: %s", player.name, r["host"], e)
                    continue
                moved.append(f"{player.name} is now at {r['host']}")
            player.mac = player.mac or mac
            r["player"] = player.name
        self._sync_player()
        return moved

    def _follow(self, names: list[str], seconds: float = 300) -> None:
        """After robots switch networks, look for them every few seconds until they've all turned up (this Mac
        has to join their new network too), following each to its new address."""
        if not names:
            return
        self._looking_for |= set(names)
        if self._follow_task and not self._follow_task.done():
            return  # the search under way looks for these too

        async def search() -> None:
            deadline = time.monotonic() + seconds
            try:
                while self._looking_for and time.monotonic() < deadline:
                    await asyncio.sleep(6)
                    with contextlib.suppress(OSError, AimError):
                        robots = await network.find_robots()
                        await self._match_found(robots)
                        for name in self._looking_for & {r.get("player") for r in robots}:
                            self._looking_for.discard(name)
                            self.state.log("robot", f"{name} is back, at {self.fleet.get(name).host}")
            finally:
                self._looking_for = set()

        self._follow_task = asyncio.get_running_loop().create_task(search())

    async def _label_robot(self) -> None:
        """The player card on the robot's screen: name, team colour and a happy face; lights to match."""
        await screen.react(self.robot, self.state, screen.RESTING)

    # ----------------------------------------------------------------------------------
    # What the page shows
    # ----------------------------------------------------------------------------------

    def _state_json(self, since: int) -> dict:
        robot, state = self.robot, self.state
        now = time.monotonic()
        info: dict = {"team": state.team, "labels": state.labels, "selected": state.selected,
                      "colours": state.colours, "fps": round(self._fps, 1), "event_seq": state.event_seq,
                      "status_hz": round(robot.status_rate, 1),
                      "events": [e for e in state.events if e["seq"] > since],
                      "yolo": {"enabled": self.use_yolo, "ready": bool(self.yolo and self.yolo.ready)},
                      "trail": [[round(x), round(y)] for x, y in self._trail], "detections": [],
                      "fov_deg": FOV_DEG, "camera_range": state.camera_range(),
                      "field": state.field, "tags": state.tags, "obstacle_mm": OBSTACLE_MM,
                      "routine": state.routine_status(), "abilities": state.abilities, "kick_reach": state.kick_reach(),
                      "last_kick": state.last_kick, "player": state.player, "max_speed": routines.MAX_SPEED_PERCENT,
                      "battery_trend": state.battery_trend(), "mode": state.mode, "aim_controller": self._aim_controller(),
                      "players": [{**p, "selected": p["name"] == self.fleet.selected} for p in self.fleet.statuses()],
                      "match": {**state.match, "seconds_left": max(0, round(state.match["ends"] - time.time(), 1))} if state.match else None,
                      "map": [{"display": o["display"], "kind": o["kind"], "name": o["name"], "id": o["id"],
                               "x": round(o["x"]), "y": round(o["y"]), "visible": now - o["seen"] < 0.8,
                               "vx": round(o.get("vx", 0)) if now - o["seen"] < 1.5 else 0,
                               "vy": round(o.get("vy", 0)) if now - o["seen"] < 1.5 else 0,
                               "sightings": o["sightings"]} for o in state.map if o["sightings"] >= MAP_CONFIRM]}
        info["released"] = self._released
        info["assistant"] = state.assistant
        info["looking_for"] = sorted(self._looking_for)
        if not robot.connected:
            self._maybe_connect()
            info["robot"] = {"connected": False, "problem": "Let go, so the robot's own Drive mode and menus work: ▾ Controllers → Back to the panel, when you've finished."
                             if self._released else robot.connection_problem()}
            return info
        try:
            snap = robot.snapshot()
        except AimError as e:
            info["robot"] = {"connected": False, "problem": str(e)}
            return info
        heading = robot.heading
        with contextlib.suppress(Exception):  # the same advice the advise tool gives (strategy.decide)
            holding = held_object(robot.detections()) == "SportsBall"
            info["advice"] = strategy.decide(state, (*state.on_field(*robot.position), heading), holding)
        info["robot"] = {"connected": True, "host": robot.host, "battery": snap["battery_percent"],
                         "heading": heading, "x": snap["position_mm"]["x"], "y": snap["position_mm"]["y"],
                         "moving": snap["moving"], "holding": snap["holding"], "motion_enabled": robot.motion_enabled,
                         "apriltags": robot.apriltags_on, "touched": snap["screen_touched"]}
        for d in robot.detections():
            info["detections"].append({
                "source": "onboard", "kind": d.kind, "name": d.name, "id": d.id,
                "display": state.display_name(d, heading), "label": state.label_for(d, heading),
                "bearing": round(d.bearing, 1), "heading": round((heading + d.bearing) % 360, 1),
                "box": [d.x * 2, d.y * 2, (d.x + d.width) * 2, (d.y + d.height) * 2],
                "size": [d.width, d.height], "distance_mm": approx_distance_mm(d, state), "held": is_held(d)})
        for b in self._yolo_boxes:
            info["detections"].append({"source": "yolo", "kind": "yolo", "name": b.label, "id": None,
                                       "display": b.label, "label": None, "bearing": round(b.bearing, 1),
                                       "score": round(b.score * 100) if b.score is not None else None,
                                       "heading": round((heading + b.bearing) % 360, 1),
                                       "box": [round(v) for v in (b.x0, b.y0, b.x1, b.y1)], "size": None,
                                       "distance_mm": None, "held": False})
        return info

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                     b"Cache-Control: no-store\r\nConnection: close\r\n\r\n")
        await writer.drain()
        count, window_start = 0, time.monotonic()
        try:
            async with contextlib.aclosing(self.robot.frames()) as frames:
                # Frames that arrive while one is being processed are skipped, so the view stays live.
                # The frames go out untouched: the page draws the boxes, labels and ruler itself.
                async for jpeg in frames:
                    self._yolo_boxes = []
                    if self.use_yolo and self.yolo:
                        if self.yolo.ready:
                            try:
                                self._yolo_boxes = await self.yolo.detect(jpeg)
                            except AimError as e:
                                log.warning("%s", e)
                        else:
                            self.yolo.warm_up()
                    writer.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg)
                                 + jpeg + b"\r\n")
                    await writer.drain()
                    count += 1
                    if (elapsed := time.monotonic() - window_start) >= 1:
                        self._fps, count, window_start = count / elapsed, 0, time.monotonic()
        finally:
            self._fps = 0.0


async def _main() -> None:
    parser = argparse.ArgumentParser(description="VEX AIM control panel in a browser.")
    parser.add_argument("--host", default=os.environ.get("AIM_HOST", "192.168.4.1"), help="robot IP or hostname")
    parser.add_argument("--port", type=int, default=8765, help="local port for the page")
    parser.add_argument("--no-yolo", action="store_true", help="only draw the robot's own detections")
    parser.add_argument("--model", default=os.environ.get("AIM_YOLO_MODEL", DEFAULT_YOLO_MODEL), help="YOLO weights")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = parser.parse_args()

    robot = AimRobot(args.host)
    panel = ControlPanel(robot, PanelState(), args.port, YoloDetector(args.model))
    url = await panel.start(use_yolo=not args.no_yolo)
    print(f"Control panel at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await panel.stop()
        if panel.yolo:
            await panel.yolo.close()
        await robot.disconnect()


def main() -> None:
    """The vex-aim-panel command: the control panel on its own, without an AI assistant."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


if __name__ == "__main__":
    main()
