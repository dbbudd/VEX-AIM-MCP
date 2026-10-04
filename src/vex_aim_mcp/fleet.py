"""The team list: several AIM robots, each a named player on the blue or orange team (or neither),
with its own connection.

Every robot measures where it is from where it stood when it connected (its odometry frame), so the
fleet puts them all on one playing field, in world.py's field coordinates: (0, 0) at the centre, x to
the right, y up the field, heading 0 facing up the field. A player's start position (where it stands,
and which way it faces, when it first connects) ties its odometry to the field. After a dropped
connection a robot carries on from where it was last seen, and place() sets its position by hand, e.g.
when everyone goes back to their start spots for a kickoff.

With every player on the field, a "Robot" that one of them sees can be identified: where the camera
puts it is matched to the nearest player. One that matches nobody isn't on the team list, so it's
probably an opponent's.

Shared by the control panel and Claude's tools; it only needs aim_client and world.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

from .aim_client import AimError, AimRobot, Detection, held_object
from .world import FOCAL_PX, SIZE_K, PanelState, approx_distance_mm, cut_off, wrap180

log = logging.getLogger("vex_aim.fleet")

TEAMS = ("blue", "orange")
TEAM_RGB = {"blue": (0, 90, 255), "orange": (255, 110, 0), None: (40, 40, 40)}
NAME_MAX = 16  # characters, to fit on the robot's 240-px screen
# The biggest font each length of name fits in on one line: (longest name, font, line height).
NAME_FONTS = ((6, "mono60", 64), (9, "mono40", 44), (12, "mono30", 34), (NAME_MAX, "mono20", 24))
# The camera sees a robot's near side; its centre is half a robot's width (140 mm) further on.
ROBOT_HALF_MM = SIZE_K["Robot"] / FOCAL_PX / 2
# A sighting within MATCH_MM of where a player is (or MATCH_SHARE of its distance, if more: distances
# from apparent size get rougher further away) is that player. A robot cut off at the edge of the
# picture has no distance, so it's matched by direction alone, within MATCH_DEG.
MATCH_MM = 250
MATCH_SHARE = 0.3
MATCH_DEG = 10
START_HELP = ("A position on the field is x and y in mm (x to the right, y up the field, (0, 0) at the centre) "
              "and a heading in degrees (0 = facing up the field, 180 = facing down it).")

Card = Callable[[AimRobot, str, str | None], Awaitable[None]]  # draws a player's label: (robot, name, team)


def clean_name(name: str) -> str:
    """A player's name with its spaces tidied, or an AimError saying what's wrong with it."""
    name = " ".join(str(name or "").split())
    if not name:
        raise AimError("Give the player a name.")
    if len(name) > NAME_MAX:
        raise AimError(f"“{name}” is too long: a player's name can have at most {NAME_MAX} characters, "
                       "to fit on the robot's screen.")
    return name


def clean_host(host: str) -> str:
    host = str(host or "").strip()
    if not host or any(c.isspace() for c in host) or "/" in host:
        raise AimError(f"“{host}” isn't a robot's address. Use its IP address, e.g. 192.168.1.50 "
                       "(the robot shows it under Settings → Radio).")
    return host


def clean_team(team: str | None) -> str | None:
    if team is None or not str(team).strip():
        return None
    if str(team).strip().lower() not in TEAMS:
        raise AimError(f"The team must be blue or orange (or none), not “{team}”.")
    return str(team).strip().lower()


def clean_pose(pose) -> tuple[float, float, float]:
    """A field pose from (x, y, heading), (x, y) or {"x", "y", "heading"}; heading 0 if not given."""
    try:
        if isinstance(pose, dict):
            pose = (pose["x"], pose["y"], pose.get("heading", 0))
        x, y, heading = (float(v) for v in (*pose, 0)[:3])
    except (KeyError, TypeError, ValueError):
        raise AimError(START_HELP) from None
    if not all(math.isfinite(v) for v in (x, y, heading)):
        raise AimError(START_HELP)
    return x, y, heading % 360


def _rotate(x: float, y: float, degrees: float) -> tuple[float, float]:
    """Turn a vector clockwise, as headings turn (x is to the right of y)."""
    a = math.radians(degrees)
    return x * math.cos(a) + y * math.sin(a), y * math.cos(a) - x * math.sin(a)


class Player:
    """One robot on the team list: its name, address and team, where it starts on the field, and its
    own connection (made when it's first needed)."""

    def __init__(self, name: str, host: str, team: str | None = None, start=None,
                 robot: AimRobot | None = None, idle_disconnect_s: float = 0) -> None:
        self.name = clean_name(name)
        self.host = clean_host(host)
        self.team = team
        self.idle_disconnect_s = idle_disconnect_s
        self._robot = robot
        self._start: tuple[float, float, float] | None = clean_pose(start) if start is not None else None
        self.last_pose: tuple[float, float, float] | None = None  # field x, y, heading, when last worked out
        self.located_by: str | None = None  # how its field position is known
        self._frame: tuple[float, float, float] | None = None  # odometry -> field: turn by [2], then shift
        self._frame_connection: int | None = None  # the robot's connection_count that _frame belongs to
        self._dropped = False  # the connection dropped since: a new one may be on its way
        self.mac: str | None = None  # its radio's hardware address: recognises it at a new address (another network)

    def __repr__(self) -> str:
        return f"Player({self.name!r}, {self.host!r}, team={self.team!r})"

    @property
    def team(self) -> str | None:
        return self._team

    @team.setter
    def team(self, team: str | None) -> None:
        self._team = clean_team(team)

    @property
    def start(self) -> tuple[float, float, float] | None:
        """Where the robot stands on the field when it first connects: x, y (mm) and heading."""
        return self._start

    @start.setter
    def start(self, pose) -> None:
        # A connected robot whose field position came from its start position (or that had none) is
        # measured from the new one: it stood on its start spot when it connected, wherever that was.
        self._start = clean_pose(pose) if pose is not None else None
        self.field_pose()  # catch up with a reconnect first
        if self._frame_connection is not None and self.located_by in (None, "its start position"):
            self._frame, self.last_pose = self._start, None
            self.located_by = "its start position" if self._start else None
            self.field_pose()

    @property
    def robot(self) -> AimRobot:
        if self._robot is None:
            self._robot = AimRobot(self.host, idle_disconnect_s=self.idle_disconnect_s)
        return self._robot

    @property
    def connected(self) -> bool:
        return self._robot is not None and self._robot.connected

    # ----------------------------------------------------------------------------------
    # Where it is on the field
    # ----------------------------------------------------------------------------------

    def field_pose(self) -> tuple[float, float, float] | None:
        """Where the robot is on the field: x, y (mm) and heading (degrees, 0 = facing up the field).
        None if that isn't known. While it's disconnected, where it was last seen."""
        r = self._robot
        if r is None or not r.connection_count:
            return self.last_pose
        if r.connection_count != self._frame_connection:
            self._new_frame(r.connection_count)
        elif not r.connected:
            self._dropped = True  # its last snapshot still fits the frame, so carry on
        elif self._dropped:
            return self.last_pose  # reconnecting: its newest snapshot may not fit the old frame
        if self._frame is None:
            return None
        try:
            (ox, oy), oh = r.position, r.heading
        except (AimError, KeyError, ValueError):
            return self.last_pose
        tx, ty, turn = self._frame
        fx, fy = _rotate(ox, oy, turn)
        self.last_pose = (tx + fx, ty + fy, (oh + turn) % 360)
        return self.last_pose

    def _new_frame(self, connection: int) -> None:
        """A new connection measures from where the robot is now: the first time, its start position;
        after that, carry on from where it was last seen."""
        if self._frame_connection is None:
            anchor, how = self._start, "its start position" if self._start else None
        else:
            anchor, how = self.last_pose, "where it was before it reconnected" if self.last_pose else None
        self._frame, self.located_by = anchor, how  # the odometry's (0, 0), heading 0, is the anchor
        self._frame_connection, self._dropped = connection, False

    def to_field(self, x: float, y: float) -> tuple[float, float] | None:
        """A point in the robot's odometry frame (e.g. a map entry), in field coordinates. None if the
        robot isn't on the field."""
        if self.field_pose() is None or self._frame is None:
            return None
        tx, ty, turn = self._frame
        fx, fy = _rotate(x, y, turn)
        return tx + fx, ty + fy

    def locate(self, d: Detection, state: PanelState | None = None) -> tuple[float, float] | None:
        """Where something this player's camera sees is on the field, from its apparent size. None if
        that can't be worked out: the player isn't on the field, the object is cut off at the edge of
        the picture (so its size is wrong), or it's a kind with no known size."""
        pose = self.field_pose()
        mm = approx_distance_mm(d, state) if not cut_off(d) else None
        if pose is None or not mm:
            return None
        if d.kind == "ai_object" and d.name == "Robot":
            mm += ROBOT_HALF_MM
        a = math.radians(pose[2] + d.bearing)
        return pose[0] + mm * math.sin(a), pose[1] + mm * math.cos(a)

    def place(self, x: float, y: float, heading: float = 0) -> None:
        """Say where the robot is on the field right now, e.g. after putting it on a spot by hand."""
        r = self._robot
        if r is None or not r.connected:
            raise AimError(f"{self.name} isn't connected, so it can't be placed on the field. Connect it first.")
        x, y, heading = clean_pose((x, y, heading))
        (ox, oy), oh = r.position, r.heading
        turn = (heading - oh) % 360
        fx, fy = _rotate(ox, oy, turn)
        self._frame, self.last_pose = (x - fx, y - fy, turn), (x, y, heading)
        self._frame_connection, self._dropped = r.connection_count, False
        self.located_by = "placed by hand"

    def place_at_start(self) -> None:
        """The robot is back on its start spot (e.g. for a kickoff)."""
        if not self._start:
            raise AimError(f"{self.name} has no start position yet.")
        self.place(*self._start)
        self.located_by = "back at its start position"

    # ----------------------------------------------------------------------------------
    # Its robot
    # ----------------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect if it isn't connected, keeping track of where it is on the field."""
        self.field_pose()  # note where it is, in case this is a reconnect
        await self.robot.ensure_connected()
        self.field_pose()

    async def disconnect(self) -> None:
        if self._robot is not None:
            self.field_pose()
            await self._robot.disconnect()

    async def stop(self) -> None:
        """Stop its wheels, reconnecting if the connection dropped: a robot may not stop by itself when
        its Wi-Fi drops mid-move. One never connected isn't moving for us, so there's nothing to do."""
        if self._robot is None:
            return
        await self.connect()
        await self.robot.stop()

    async def label(self, card: Card | None = None) -> None:
        """Show the player's name on its robot's screen, on its team colour, and light it up to match.
        card(robot, name, team) draws it instead, e.g. screen.show_player_card (which adds a face)."""
        await self.connect()
        if card:
            await card(self.robot, self.name, self.team)
            return
        rgb = TEAM_RGB[self.team]
        font, line_height = next((f, h) for longest, f, h in NAME_FONTS if len(self.name) <= longest)
        await self.robot.show_text([self.name], font, (255, 255, 255), rgb, line_height)
        await self.robot.set_led("all", rgb)

    def status(self) -> dict:
        """Name, team, whether it's connected, battery, and where it is on the field. While it's
        disconnected, the battery and position it last had."""
        r, pose = self._robot, self.field_pose()
        out = {"name": self.name, "team": self.team, "host": self.host, "connected": self.connected,
               "battery_percent": None, "field_mm": [round(pose[0]), round(pose[1])] if pose else None,
               "heading_deg": round(pose[2], 1) if pose else None, "located_by": self.located_by,
               "motion_enabled": bool(r and r.motion_enabled), "holding": None}
        if r is not None and r.connection_count:
            try:
                out["battery_percent"] = r.status["robot"].get("battery")
                out["holding"] = held_object(r.detections()) if r.connected else None
            except (AimError, KeyError):
                pass
        if not self.connected:
            out["problem"] = r.connection_problem() if r else "Not connected yet."
        elif pose is None:
            out["problem"] = "Its start position isn't set, so it isn't on the map yet: set it, or place it on the field."
        return out

    def to_dict(self) -> dict:
        start = self._start and {"x": round(self._start[0], 1), "y": round(self._start[1], 1),
                                 "heading": round(self._start[2], 1)}
        return {"name": self.name, "host": self.host, "team": self.team, "start": start, **({"mac": self.mac} if self.mac else {})}


@dataclass
class RobotSighting:
    """A robot one player can see, and which player it is, if the team list can tell."""

    bearing_deg: float  # left (-) or right (+) of straight ahead
    distance_mm: float | None  # from the camera, by its apparent size; None if it's cut off at the picture's edge
    field_xy: tuple[float, float] | None  # where the camera puts its centre on the field
    player: Player | None = None  # None: not on the team list (probably an opponent's), or no way to tell
    relation: str = "unknown"  # "teammate", "opponent", "player" (no teams to compare) or "unknown"
    off_by_mm: float | None = None  # between field_xy and where that player thinks it is

    def as_dict(self) -> dict:
        out = {"player": self.player.name if self.player else None, "relation": self.relation,
               "bearing_deg": round(self.bearing_deg, 1), "distance_mm": self.distance_mm,
               "field_mm": [round(v) for v in self.field_xy] if self.field_xy else None}
        if self.player:
            out["team"] = self.player.team
            if self.off_by_mm is not None:
                out["off_by_mm"] = round(self.off_by_mm)
        return out


class Fleet:
    """The team list: players by name, connected, stopped and labelled together, and able to tell
    teammates from opponents."""

    def __init__(self, idle_disconnect_s: float = 0) -> None:
        self.players: list[Player] = []
        self.selected: str | None = None  # the player to use when none is named
        self.idle_disconnect_s = idle_disconnect_s

    def __iter__(self) -> Iterator[Player]:
        return iter(self.players)

    def __len__(self) -> int:
        return len(self.players)

    def names(self) -> list[str]:
        return [p.name for p in self.players]

    # ----------------------------------------------------------------------------------
    # The team list
    # ----------------------------------------------------------------------------------

    def find(self, name: str) -> Player | None:
        """The player with this name (ignoring capitals and extra spaces), if any."""
        key = " ".join(str(name or "").split()).casefold()
        return next((p for p in self.players if p.name.casefold() == key), None)

    def get(self, name: str) -> Player:
        if player := self.find(name):
            return player
        raise AimError(f"There's no player called “{name}”. "
                       + (f"The team list has {', '.join(self.names())}." if self.players else "The team list is empty."))

    def pick(self, name: str | None = None) -> Player:
        """The named player; or else the selected one, or the only one."""
        if name:
            return self.get(name)
        if self.selected and (player := self.find(self.selected)):
            return player
        if len(self.players) == 1:
            return self.players[0]
        raise AimError(f"Say which player: {', '.join(self.names())}." if self.players
                       else "The team list is empty. Add a player first.")

    def select(self, name: str | None) -> Player | None:
        """Choose the player to use when none is named (None: no choice)."""
        player = self.get(name) if name else None
        self.selected = player.name if player else None
        return player

    def _check_unique(self, name: str, host: str, ignore: Player | None = None) -> None:
        for p in self.players:
            if p is ignore:
                continue
            if p.name.casefold() == name.casefold():
                raise AimError(f"There's already a player called “{p.name}”.")
            if p.host.casefold() == host.casefold():
                raise AimError(f"{p.name} already uses the robot at {host}.")

    def add(self, name: str, host: str, team: str | None = None, start=None, *,
            robot: AimRobot | None = None) -> Player:
        """Put a robot on the team list. start is where it stands on the field when it first connects:
        (x, y, heading). robot is an existing connection to that host to share, e.g. the server's."""
        player = Player(name, host, team, start, robot, self.idle_disconnect_s)
        if robot is not None and robot.host != player.host:
            raise AimError(f"That connection is to {robot.host}, not {player.host}.")
        self._check_unique(player.name, player.host)
        self.players.append(player)
        return player

    async def remove(self, name: str) -> None:
        """Take a player off the team list, disconnecting its robot."""
        player = self.get(name)
        self.players.remove(player)
        if self.selected == player.name:
            self.selected = None
        try:
            await player.disconnect()
        except AimError as e:
            log.warning("%s: %s", player.name, e)

    def rename(self, name: str, new_name: str) -> Player:
        player = self.get(name)
        new_name = clean_name(new_name)
        self._check_unique(new_name, player.host, ignore=player)
        if self.selected == player.name:
            self.selected = new_name
        player.name = new_name
        return player

    async def set_host(self, name: str, host: str) -> Player:
        """Give a player a different robot (another address). It starts afresh: a new connection, and
        on the field from its start position once it connects."""
        old = self.get(name)
        new = Player(old.name, host, old.team, old.start, idle_disconnect_s=self.idle_disconnect_s)
        new.mac = old.mac
        self._check_unique(new.name, new.host, ignore=old)
        await old.disconnect()
        self.players[self.players.index(old)] = new
        return new

    def to_dict(self) -> list[dict]:
        """The team list as plain data, for saving: each player's name, host, team and start position."""
        return [p.to_dict() for p in self.players]

    @classmethod
    def from_dict(cls, data: list[dict] | None, idle_disconnect_s: float = 0) -> Fleet:
        """A fleet from to_dict()'s data. Entries that don't make sense are skipped (and logged)."""
        fleet = cls(idle_disconnect_s)
        for entry in data or []:
            try:
                fleet.add(entry["name"], entry["host"], entry.get("team"), entry.get("start")).mac = entry.get("mac")
            except (AimError, KeyError, TypeError, AttributeError) as e:
                log.warning("skipped a player in the saved team list (%s): %r", e, entry)
        return fleet

    # ----------------------------------------------------------------------------------
    # All the robots at once. One robot's trouble never stops the others: each result is
    # None if it worked, or what went wrong.
    # ----------------------------------------------------------------------------------

    async def _each(self, job: Callable[[Player], Awaitable[None]]) -> dict[str, str | None]:
        players = list(self.players)

        async def one(player: Player) -> str | None:
            try:
                await job(player)
                return None
            except AimError as e:
                return str(e)
            except Exception as e:
                log.warning("%s: %s", player.name, e, exc_info=True)
                return f"Something went wrong with {player.name} ({type(e).__name__}: {e})."

        return dict(zip((p.name for p in players), await asyncio.gather(*(one(p) for p in players))))

    async def connect_all(self) -> dict[str, str | None]:
        """Connect every player's robot at once; those already connected stay as they are."""
        return await self._each(Player.connect)

    async def disconnect_all(self) -> dict[str, str | None]:
        return await self._each(Player.disconnect)

    async def stop_all(self) -> dict[str, str | None]:
        """Stop every robot's wheels at once (reconnecting any that dropped)."""
        return await self._each(Player.stop)

    async def label_all(self, card: Card | None = None) -> dict[str, str | None]:
        """Show each player's name on its robot's screen, on its team colour, with lights to match
        (card: see Player.label)."""
        return await self._each(lambda p: p.label(card))

    def place_at_starts(self) -> dict[str, str | None]:
        """Everyone is back on their start spot (e.g. for a kickoff): set their field positions to match."""
        out: dict[str, str | None] = {}
        for p in self.players:
            try:
                p.place_at_start()
                out[p.name] = None
            except AimError as e:
                out[p.name] = str(e)
        return out

    def statuses(self) -> list[dict]:
        """Each player's name, team, connection, battery and position on the field."""
        return [p.status() for p in self.players]

    # ----------------------------------------------------------------------------------
    # Teammate or opponent?
    # ----------------------------------------------------------------------------------

    def robots_in_view(self, seer: Player | str, state: PanelState | None = None) -> list[RobotSighting]:
        """Every robot the player's camera sees right now, and which player each one is."""
        seer = seer if isinstance(seer, Player) else self.get(seer)
        if not seer.connected:
            raise AimError(f"{seer.name} isn't connected.")
        dets = [d for d in seer.robot.detections() if d.kind == "ai_object" and d.name == "Robot"]
        return self._identify(seer, dets, state)

    def identify(self, seer: Player | str, detection: Detection, state: PanelState | None = None) -> RobotSighting:
        """Which player a "Robot" that seer's camera sees is: the one nearest to where the camera puts
        it, within a tolerance. None (relation "unknown") if no player is there: it isn't on the team
        list, so probably an opponent's. field_xy says where it is, either way."""
        return self._identify(seer if isinstance(seer, Player) else self.get(seer), [detection], state)[0]

    def _identify(self, seer: Player, dets: list[Detection], state: PanelState | None) -> list[RobotSighting]:
        pose = seer.field_pose()
        found = [RobotSighting(d.bearing, approx_distance_mm(d, state) if not cut_off(d) else None,
                               seer.locate(d, state)) for d in dets]
        if pose is None:
            return found
        others = [(p, p.field_pose()) for p in self.players if p is not seer]
        others = [(p, (xy[0], xy[1])) for p, xy in others if xy]
        options = []  # (how good a match: 0 best, 1 worst; sighting; player; mm off)
        for s in found:
            if s.field_xy:
                tolerance = max(MATCH_MM, MATCH_SHARE * s.distance_mm)
                for p, xy in others:
                    off = math.dist(s.field_xy, xy)
                    if off <= tolerance:
                        options.append((off / tolerance, s, p, off))
            else:  # cut off at the picture's edge, so no distance: by direction, if only one player fits
                direction = pose[2] + s.bearing_deg
                fits = []
                for p, (x, y) in others:
                    off = abs(wrap180(math.degrees(math.atan2(x - pose[0], y - pose[1])) - direction))
                    if off <= MATCH_DEG:
                        fits.append((off, p))
                if len(fits) == 1:
                    options.append((fits[0][0] / MATCH_DEG, s, fits[0][1], None))
        for _, s, p, off in sorted(options, key=lambda o: o[0]):  # best matches first; each robot is one player
            if s.player is None and all(f.player is not p for f in found):
                s.player, s.off_by_mm = p, off
                s.relation = ("player" if not (seer.team and p.team) else
                              "teammate" if seer.team == p.team else "opponent")
        return found
