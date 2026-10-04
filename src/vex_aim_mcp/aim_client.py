"""Async client for the VEX AIM robot's Wi-Fi WebSocket interface.

VEX doesn't document the wire protocol; this follows VEX's official Python library
(AIM_Websocket_Library v1.0.1: vex/aim.py and vex/vex_messages.py). The robot serves
four WebSockets on port 80:

  /ws_cmd     JSON commands, each sent as a *binary* frame. The robot answers every
              command with one JSON reply: {"cmd_id", "status": "complete" |
              "in_progress" | "error", "error_info"?}.
  /ws_status  Send the byte 0x01 and the robot replies with a JSON snapshot: IMU,
              odometry, status flags, touchscreen, battery, and the detections from
              its onboard AI vision.
  /ws_img     Send 0x01 to start and 0x00 to stop a stream of 640x480 JPEG frames.
  /ws_audio   A 64-byte header followed by a WAV or MP3 file (max 255 KB) to play.

Unlike the official library, this client is asyncio-native, never prints to stdout,
never exits the process, serialises access to the command socket, and treats a lost
connection as a state to report rather than a crash. A long-running MCP server needs
all of that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.protocol import State

log = logging.getLogger("vex_aim.client")

# Bits of status["robot"]["flags"] (a hex string), as defined in vex/aim.py.
FLAG_SOUND_PLAYING = 1 << 0
FLAG_MOVE_ACTIVE = 1 << 1
FLAG_IMU_CALIBRATING = 1 << 3
FLAG_TURN_ACTIVE = 1 << 4
FLAG_MOVING = 1 << 5  # any wheel turning
FLAG_CRASHED = 1 << 6
FLAG_POWER_BUTTON = 1 << 9
FLAG_PROGRAM_ACTIVE = 1 << 10
FLAG_SOUND_DOWNLOADING = 1 << 16

DRIVE_MAX_MMPS = 200  # 100 % drive speed
TURN_MAX_DPS = 180  # 100 % turn speed
AUDIO_MAX_BYTES = 255 * 1024
VISION_WIDTH, VISION_HEIGHT = 320, 240  # coordinate space of onboard detections

# status["aivision"]["objects"]["items"][i]["type"]
TYPE_COLOR, TYPE_CODE, TYPE_AI, TYPE_TAG = 1, 2, 4, 8

# Built-in sounds (vex_types.SoundType values, sent lower-case).
SOUNDS = (
    "doorbell", "tada", "fail", "sparkle", "flourish", "forward", "reverse", "right", "left",
    "blinker", "crash", "brakes", "huah", "pickup", "cheer", "sensing", "detected", "obstacle",
    "looping", "complete", "pause", "resume", "send", "receive",
    "act_happy", "act_sad", "act_excited", "act_angry", "act_silly",
)

# Animated emoji, in vex_types.EmojiType order (the robot takes the index).
EMOJI = (
    "excited", "confident", "silly", "amazed", "strong", "thrilled", "happy", "proud",
    "laughing", "optimistic", "determined", "affectionate", "calm", "quiet", "shy", "cheerful",
    "loved", "surprised", "thinking", "tired", "confused", "bored", "embarrassed", "worried",
    "sad", "sick", "disappointed", "nervous", "annoyed", "stressed", "angry", "frustrated",
    "jealous", "shocked", "fear", "disgust",
)
EMOJI_LOOK = ("forward", "left", "right")

# Screen fonts are "mono" or "prop" plus a pixel size.
FONTS = ("mono12", "mono15", "mono20", "mono24", "mono30", "mono36", "mono40", "mono60",
         "prop20", "prop24", "prop30", "prop36", "prop40", "prop60")


class AimError(Exception):
    """A problem talking to the robot, worded for the person using it."""


def bearing_deg(cx: float, cy: float) -> float:
    """Horizontal angle from the camera's centre line to a point in vision coordinates
    (320x240). Negative = left, positive = right. VEX's calibration from vex/aim.py."""
    return (-34.656 + cx * 0.22539 + cy * 0.011526 + cx * cx * -0.000042011
            + cx * cy * 0.000010433 + cy * cy * -0.00007073)


def vision_x_for_bearing(bearing: float, cy: float = VISION_HEIGHT / 2) -> float:
    """Inverse of bearing_deg along a horizontal line (bisection: it rises steadily with x)."""
    lo, hi = 0.0, float(VISION_WIDTH)
    for _ in range(40):
        mid = (lo + hi) / 2
        if bearing_deg(mid, cy) < bearing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass
class Detection:
    """One object seen by the robot's onboard AI vision, in 320x240 vision coordinates."""

    kind: str  # "ai_object", "apriltag", "color" or "color_code"
    name: str  # e.g. "BlueBarrel", "AprilTag 3"
    id: int
    x: int  # top-left corner
    y: int
    width: int
    height: int
    score: float | None = None  # AI objects only
    angle: float | None = None  # colors and color codes only

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def bearing(self) -> float:
        return bearing_deg(*self.center)

    def as_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "name": self.name,
            "bearing_deg": round(self.bearing, 1),
            "center": [round(c) for c in self.center],
            "size": [self.width, self.height],
        }
        if self.score is not None:
            d["score"] = self.score
        if self.angle is not None:
            d["angle_deg"] = round(self.angle, 1)
        return d


def parse_detections(status: dict) -> list[Detection]:
    """Onboard detections from a status snapshot, largest first (as vex/aim.py sorts them)."""
    vision = status.get("aivision") or {}
    classnames = {c.get("index"): c.get("name") for c in (vision.get("classnames") or {}).get("items", [])}
    objects = vision.get("objects") or {}
    # "items" can hold a placeholder entry when count is 0, so trust count.
    items = (objects.get("items") or [])[: int(objects.get("count") or 0)]
    found = []
    for o in items:
        kind_id, oid = o.get("type"), int(o.get("id", 0))
        if kind_id == TYPE_AI:
            kind, name = "ai_object", classnames.get(oid) or f"class {oid}"
        elif kind_id == TYPE_TAG:
            kind, name = "apriltag", f"AprilTag {oid}"
        elif kind_id == TYPE_COLOR:
            kind, name = "color", f"Color {oid}"
        elif kind_id == TYPE_CODE:
            kind, name = "color_code", f"Color code {oid}"
        else:
            kind, name = "unknown", f"type {kind_id} id {oid}"
        found.append(Detection(
            kind, name, oid,
            int(o.get("originx", 0)), int(o.get("originy", 0)),
            int(o.get("width", 0)), int(o.get("height", 0)),
            score=o.get("score") if kind_id == TYPE_AI else None,
            angle=o.get("angle", 0) * 0.01 if kind_id in (TYPE_COLOR, TYPE_CODE) else None,
        ))
    found.sort(key=lambda d: d.width * d.height, reverse=True)
    return found


def is_held(d: Detection) -> bool:
    """vex/aim.py's has_*_barrel / has_sports_ball rule: the object sits low and centred in
    the camera's view, i.e. in the kicker. In dim light the onboard AI often stops seeing a
    ball once it's this close, so a False here is not proof the kicker is empty."""
    if d.kind != "ai_object" or not 120 < d.x + d.width / 2 < 200:
        return False
    if d.name in ("BlueBarrel", "OrangeBarrel"):
        return d.y > 160
    return d.name == "SportsBall" and d.y > 170


def held_object(detections: list[Detection]) -> str | None:
    """What the kicker seems to be holding, if the onboard AI can see it."""
    return next((d.name for d in detections if is_held(d)), None)


def parse_note(token: str) -> tuple[int, int]:
    """"C5", "F#6", "Bb7" -> (semitone 0-11, octave index 0-3), mirroring vex/aim.py."""
    t = token.strip()
    semitones = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
    if not t or t[0].lower() not in semitones or len(t) not in (2, 3) or t[-1] not in "5678":
        raise AimError(f"'{token}' isn't a note the robot can play. Use a letter A-G, optional # or b, "
                       "and an octave from 5 to 8, e.g. C5, F#6, Bb7.")
    note = semitones[t[0].lower()]
    if len(t) == 3:
        if t[1] == "#" and note < 11:
            note += 1
        elif t[1] == "b" and note > 0:
            note -= 1
        elif t[1] not in "#b":
            raise AimError(f"'{token}': the middle character must be # or b.")
    return note, int(t[-1]) - 5


class _Latest:
    """The most recent value from a stream, plus a way to wait for a newer one."""

    def __init__(self) -> None:
        self.value = None
        self.seq = 0
        self.time = 0.0
        self._cond = asyncio.Condition()

    async def put(self, value) -> None:
        async with self._cond:
            self.value, self.seq, self.time = value, self.seq + 1, time.monotonic()
            self._cond.notify_all()

    async def wait_newer(self, seq: int, timeout: float):
        async with self._cond:
            await asyncio.wait_for(self._cond.wait_for(lambda: self.seq > seq), timeout)
            return self.value


async def _drain(ws: ClientConnection, settle: float = 0.001) -> int:
    """Throw away messages already waiting on a socket, e.g. a reply that arrived after
    its command had timed out, so the next reply lines up with the next command."""
    dropped = 0
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=settle)
        except TimeoutError:
            return dropped
        dropped += 1
        if not isinstance(msg, bytes):
            log.info("discarded unread message: %.200s", msg)


class AimRobot:
    """One robot, reached over Wi-Fi. Connects on demand; all methods are coroutines."""

    STATUS_PERIOD = 0.1  # seconds between status polls (the official library uses 0.05)
    COMMAND_TIMEOUT = 3.0
    STREAM_LINGER = 5.0  # keep the camera streaming this long after the last request

    def __init__(self, host: str, idle_disconnect_s: float = 0) -> None:
        self.host = host
        self.idle_disconnect_s = idle_disconnect_s
        self.motion_enabled = False  # unlocked per connection, after the user confirms it's safe
        self.apriltags_on = False  # the robot starts with AprilTag detection off
        self.last_activity = time.monotonic()
        self._sockets: dict[str, ClientConnection] = {}
        self._tasks: list[asyncio.Task] = []
        self._connected = False
        self._lost_reason: str | None = None
        self._program_seen_active = False
        self._cmd_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._camera_lock = asyncio.Lock()
        self._status = _Latest()
        self._status_times: deque[float] = deque(maxlen=11)
        self._frames = _Latest()
        self._image_task: asyncio.Task | None = None
        self._streaming = False
        self._viewers = 0
        self._stream_until = 0.0
        self._heading_offset = 0.0
        self._origin = (0.0, 0.0)
        self.connection_count = 0  # lets callers notice a reconnect (which resets heading/position)

    # ----------------------------------------------------------------------------------
    # Connection
    # ----------------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        # Any of the four sockets can drop on its own, so check them all.
        return (self._connected and self._lost_reason is None
                and all(ws.state is State.OPEN for ws in self._sockets.values()))

    @property
    def status_rate(self) -> float:
        """Status snapshots per second lately: how often the onboard AI's detections refresh."""
        t = self._status_times
        if len(t) < 2 or time.monotonic() - t[-1] > 1:
            return 0.0
        return (len(t) - 1) / (t[-1] - t[0])

    def connection_problem(self) -> str:
        if self._lost_reason:
            return f"Lost the connection to the robot: {self._lost_reason}."
        return f"Not connected to the robot at {self.host}."

    async def ensure_connected(self) -> None:
        if not self.connected:
            await self.connect()

    async def connect(self, host: str | None = None) -> None:
        async with self._connect_lock:
            if host and host != self.host:
                await self._teardown()
                self.host = host
            if self.connected:
                return
            await self._teardown()
            self._lost_reason = None
            try:
                # Same order as the official library: status, image, command, audio.
                for name in ("ws_status", "ws_img", "ws_cmd", "ws_audio"):
                    self._sockets[name] = await ws_connect(
                        f"ws://{self.host}/{name}",
                        open_timeout=6,
                        close_timeout=1,
                        ping_interval=None,  # the robot's server isn't known to answer pings
                        compression=None,
                        max_size=8 * 1024 * 1024,
                        proxy=None,  # it's on the local network; never route via a proxy
                    )
            except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as e:
                await self._teardown()
                raise AimError(
                    f"Couldn't reach the robot at {self.host} ({type(e).__name__}: {e}). Check that it's "
                    "switched on, in Wi-Fi Station mode, and on the same network as this computer."
                ) from e
            self._connected = True
            self._program_seen_active = False
            seq = self._status.seq  # ignore snapshots left over from an earlier connection
            self._tasks = [
                asyncio.create_task(self._status_loop(), name="aim-status"),
                asyncio.create_task(self._housekeeping_loop(), name="aim-housekeeping"),
            ]
            try:
                await self.command("program_init")  # tells the robot a remote program is starting
                status = await self._status.wait_newer(seq, timeout=5)
            except TimeoutError:
                await self._teardown()
                raise AimError("Connected, but the robot sent no status within 5 seconds.") from None
            except BaseException:
                await self._teardown()
                raise
            # Like the official library, heading 0 is wherever the robot faced at connection;
            # position is measured from where it stood (kept client-side; nothing is sent).
            self._set_origin(status)
            self.motion_enabled = False
            self.apriltags_on = False
            self.last_activity = time.monotonic()
            self.connection_count += 1
            log.info("connected to %s", self.host)

    async def disconnect(self) -> None:
        """Release the robot so VEXcode or a Python script can use it."""
        async with self._connect_lock:
            if self.connected and self.is_moving:
                try:
                    await self.stop()
                except AimError:
                    pass
            await self._stop_stream()
            await self._teardown()
            self._lost_reason = None

    def _lose(self, reason: str) -> None:
        if self._lost_reason or not self._connected:
            return
        self._lost_reason = reason
        log.warning("connection lost: %s", reason)
        asyncio.get_running_loop().create_task(self._teardown())

    async def _teardown(self) -> None:
        current = asyncio.current_task()
        tasks = [t for t in self._tasks + [self._image_task] if t and t is not current]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass
        self._tasks, self._image_task = [], None
        self._streaming = False
        sockets, self._sockets = self._sockets, {}
        for ws in sockets.values():
            try:
                await asyncio.wait_for(ws.close(), timeout=2)
            except Exception:
                pass
        self._connected = False
        self.motion_enabled = False

    async def _status_loop(self) -> None:
        ws = self._sockets["ws_status"]
        misses = 0
        while True:
            try:
                await ws.send(b"\x01")
                status = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            except ConnectionClosed:
                self._lose("the robot closed the connection")
                return
            except (TimeoutError, ValueError) as e:
                misses += 1
                log.warning("missed status packet %d (%s)", misses, type(e).__name__)
                if misses >= 5:
                    self._lose("it stopped answering (out of Wi-Fi range, asleep, or switched off?)")
                    return
                continue
            misses = 0
            await self._status.put(status)
            self._status_times.append(time.monotonic())
            self._check_flags(status)
            await asyncio.sleep(self.STATUS_PERIOD)

    def _check_flags(self, status: dict) -> None:
        flags = int(status["robot"]["flags"], 16)
        if flags & FLAG_PROGRAM_ACTIVE:
            self._program_seen_active = True
        elif self._program_seen_active:
            self._lose("the robot left remote-control mode (was its power button pressed?)")
        if flags & FLAG_POWER_BUTTON:
            self._lose("the robot's power button was pressed")

    async def _housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if self._streaming and self._viewers == 0 and now > self._stream_until:
                async with self._camera_lock:
                    if self._viewers == 0 and time.monotonic() > self._stream_until:
                        await self._stop_stream()
            if (self.idle_disconnect_s and self._viewers == 0 and not self.is_moving
                    and now - self.last_activity > self.idle_disconnect_s):
                log.info("idle for %.0f s, releasing the robot", now - self.last_activity)
                asyncio.get_running_loop().create_task(self.disconnect())
                return

    # ----------------------------------------------------------------------------------
    # Commands
    # ----------------------------------------------------------------------------------

    async def command(self, cmd_id: str, **fields) -> dict:
        """Send one command and return the robot's reply. Field order follows vex_messages.py."""
        if not self.connected:
            raise AimError(self.connection_problem())
        ws = self._sockets["ws_cmd"]
        payload = json.dumps({"cmd_id": cmd_id, **fields}, separators=(",", ":")).encode()
        async with self._cmd_lock:
            try:
                await _drain(ws)
                await ws.send(payload)  # bytes -> binary frame, as the robot expects
                raw = await asyncio.wait_for(ws.recv(), timeout=self.COMMAND_TIMEOUT)
            except TimeoutError:
                raise AimError(f"The robot didn't answer the '{cmd_id}' command in time.") from None
            except ConnectionClosed:
                self._lose("the command connection closed")
                raise AimError(self.connection_problem()) from None
        self.last_activity = time.monotonic()
        try:
            reply = json.loads(raw)
        except ValueError:
            raise AimError(f"Unexpected reply to '{cmd_id}': {raw!r:.200}") from None
        if reply.get("cmd_id") == "cmd_unknown":
            raise AimError(f"The robot doesn't recognise the command '{cmd_id}' (firmware too old?).")
        if reply.get("status") == "error":
            raise AimError(f"The robot refused '{cmd_id}': {reply.get('error_info', 'no reason given')}.")
        if reply.get("cmd_id") != cmd_id:
            log.warning("reply %s doesn't match command %s", reply, cmd_id)
        return reply

    # ----------------------------------------------------------------------------------
    # State (from the latest status snapshot)
    # ----------------------------------------------------------------------------------

    @property
    def status(self) -> dict:
        if self._status.value is None:
            raise AimError("No status received from the robot yet.")
        return self._status.value

    @property
    def status_age(self) -> float:
        return time.monotonic() - self._status.time

    @property
    def flags(self) -> int:
        return int(self.status["robot"]["flags"], 16)

    @property
    def is_moving(self) -> bool:
        try:
            return bool(self.flags & (FLAG_MOVING | FLAG_MOVE_ACTIVE | FLAG_TURN_ACTIVE))
        except AimError:
            return False

    @property
    def heading(self) -> float:
        """0-360 degrees clockwise from the direction the robot faced when connected."""
        h = math.fmod(float(self.status["robot"]["heading"]) - self._heading_offset, 360)
        return round(h + 360 if h < 0 else h, 1) % 360

    @property
    def position(self) -> tuple[float, float]:
        """Odometry in mm relative to where the robot was when connected, rotated into the
        heading frame the same way vex/aim.py does it."""
        r = self.status["robot"]
        dx, dy = float(r["robot_x"]) - self._origin[0], float(r["robot_y"]) - self._origin[1]
        a = -math.radians(self._heading_offset)
        return (round(dx * math.cos(a) + dy * math.sin(a), 1),
                round(dy * math.cos(a) - dx * math.sin(a), 1))

    def detections(self) -> list[Detection]:
        return parse_detections(self.status)

    def _set_origin(self, status: dict) -> None:
        r = status["robot"]
        self._heading_offset = float(r["heading"])
        self._origin = (float(r["robot_x"]), float(r["robot_y"]))

    def reset_origin(self) -> None:
        """Make the current heading 0° and the current position (0, 0). Client-side only."""
        self._set_origin(self.status)

    def snapshot(self) -> dict:
        """Everything worth knowing about the robot right now, as plain data."""
        s, r = self.status, self.status["robot"]
        flags = self.flags
        dets = parse_detections(s)
        x, y = self.position
        snap = {
            "host": self.host,
            "battery_percent": r.get("battery"),
            "heading_deg": self.heading,
            "position_mm": {"x": x, "y": y},
            "tilt_deg": {"roll": round(float(r.get("roll", 0)), 1), "pitch": round(float(r.get("pitch", 0)), 1)},
            "moving": bool(flags & FLAG_MOVING),
            "move_active": bool(flags & FLAG_MOVE_ACTIVE),
            "turn_active": bool(flags & FLAG_TURN_ACTIVE),
            "crash_detected": bool(flags & FLAG_CRASHED),
            "sound_playing": bool(flags & (FLAG_SOUND_PLAYING | FLAG_SOUND_DOWNLOADING)),
            "imu_calibrating": bool(flags & FLAG_IMU_CALIBRATING),
            "screen_touched": bool(int(r.get("touch_flags", "0"), 16) & 1),
            "holding": held_object(dets),
            "onboard_detections": [d.as_dict() for d in dets],
            "motion_enabled": self.motion_enabled,
            "camera_streaming": self._streaming,
            "status_age_s": round(self.status_age, 2),
        }
        if snap["screen_touched"]:
            snap["touch_xy"] = [r.get("touch_x"), r.get("touch_y")]
        return snap

    # ----------------------------------------------------------------------------------
    # Motion. Every move is bounded: the robot stops by itself at the end, and we stop it
    # early if it times out, bumps into something, or the request is cancelled.
    # ----------------------------------------------------------------------------------

    def _require_motion(self) -> None:
        if not self.motion_enabled:
            raise AimError(
                "Motion is locked for this connection. Ask the user to confirm the robot is on the "
                "floor with clear space around it, then call enable_motion."
            )

    async def stop(self) -> None:
        """Stop all wheel motion now (what the official library's stop_all_movement sends)."""
        await self.command("drive", angle=0, speed=0, stacking_type=0)
        await self.command("turn", turn_rate=0, stacking_type=0)

    async def move(self, distance_mm: float, direction_deg: float, speed_mmps: float) -> str:
        self._require_motion()
        started = time.monotonic()
        # turn_speed 75 = the official library's default turn velocity (50 %).
        await self.command("drive_for", distance=distance_mm, angle=direction_deg, final_heading=0,
                           drive_speed=speed_mmps, turn_speed=75, stacking_type=0)
        expected = abs(distance_mm) / max(speed_mmps, 1)
        return await self._wait_motion(FLAG_MOVE_ACTIVE, started, timeout=expected * 1.5 + 2)

    async def turn(self, degrees: float, speed_dps: float) -> str:
        """Positive = clockwise (right)."""
        self._require_motion()
        started = time.monotonic()
        await self.command("turn_for", angle=degrees, turn_rate=abs(speed_dps), stacking_type=0)
        expected = abs(degrees) / max(abs(speed_dps), 1)
        return await self._wait_motion(FLAG_TURN_ACTIVE, started, timeout=expected * 1.5 + 2)

    async def turn_to(self, heading_deg: float, speed_dps: float) -> str:
        self._require_motion()
        started = time.monotonic()
        raw = math.fmod(self._heading_offset + heading_deg, 360)
        await self.command("turn_to", heading=raw, turn_rate=abs(speed_dps), stacking_type=0)
        return await self._wait_motion(FLAG_TURN_ACTIVE, started, timeout=360 / max(abs(speed_dps), 1) * 1.5 + 2)

    async def _wait_motion(self, active_flag: int, started: float, timeout: float) -> str:
        """Wait for a bounded move/turn to finish. The flag can lag the command by a packet,
        so a move only counts as done once it was seen active (or a grace period passed)
        and then two packets in a row show it inactive."""
        seq, seen_active, quiet = self._status.seq, False, 0
        crash_already_set = bool(self.flags & FLAG_CRASHED)  # only react to a new bump
        try:
            while True:
                if time.monotonic() - started > timeout:
                    await self.stop()
                    return "stopped early: it took much longer than expected (blocked?)"
                try:
                    await self._status.wait_newer(seq, timeout=1.0)
                except TimeoutError:
                    continue
                seq = self._status.seq
                flags = self.flags
                if flags & FLAG_CRASHED and seen_active and not crash_already_set:
                    await self.stop()
                    return "stopped early: it bumped into something"
                if flags & active_flag:
                    seen_active, quiet = True, 0
                    continue
                quiet += 1
                if quiet >= 2 and (seen_active or time.monotonic() - started > 1.0):
                    return "completed"
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.stop())
            except Exception:
                pass
            raise

    async def drive_vector(self, forwards: float, rightwards: float, rotation: float) -> None:
        """Keep driving at these speeds, in percent (rotation positive = clockwise), until told
        otherwise. For driver control: the three omni-wheels' speeds are mixed as vex/aim.py's
        move_with_vectors does. The caller must stop the robot when the driver lets go."""
        self._require_motion()
        x, y, r = (max(-100.0, min(100.0, v)) for v in (rightwards, forwards, rotation))
        x, y, r = x * 2.0, y * 2.0, r * 1.8
        await self.command("spin_wheels", vel1=int(0.5 * x + 0.866 * y + r), vel2=int(0.5 * x - 0.866 * y + r), vel3=int(r - x))

    async def kick(self, strength: str) -> None:
        self._require_motion()
        await self.command(f"kick_{strength}")

    # ----------------------------------------------------------------------------------
    # Lights, screen, sound, vision settings
    # ----------------------------------------------------------------------------------

    async def set_led(self, led: str, rgb: tuple[int, int, int]) -> None:
        """led is "all" or "light1".."light6"."""
        r, g, b = rgb
        await self.command("light_set", **{led: {"r": r, "g": g, "b": b}})

    async def show_emoji(self, emoji: str, look: str = "forward") -> None:
        await self.command("show_emoji", name=EMOJI.index(emoji), look=EMOJI_LOOK.index(look))

    async def hide_emoji(self) -> None:
        await self.command("hide_emoji")

    async def show_text(self, lines: list[str], font: str, fg: tuple[int, int, int],
                        bg: tuple[int, int, int], line_height: int) -> None:
        await self.hide_emoji()  # an emoji would cover the text
        await self.command("lcd_clear_screen", r=bg[0], g=bg[1], b=bg[2])
        await self.command("lcd_set_font", fontname=font)
        await self.command("lcd_set_pen_color", r=fg[0], g=fg[1], b=fg[2])
        await self.command("lcd_set_fill_color", r=bg[0], g=bg[1], b=bg[2], b_transparency=False)
        for i, line in enumerate(lines):
            await self.command("lcd_print_at", x=8, y=8 + line_height * (i + 1), string=line, b_opaque=True)

    async def play_sound(self, name: str, volume: int) -> None:
        await self.command("play_sound", name=name, volume=volume)

    async def play_note(self, note: str, duration_ms: int, volume: int) -> None:
        semitone, octave = parse_note(note)
        await self.command("play_note", note=semitone, octave=octave, duration=duration_ms, volume=volume)

    async def play_audio(self, data: bytes, kind: str = "wav", volume: int = 80, name: str = "claude.wav") -> None:
        """Send a WAV or MP3 file to play on the robot's speaker (header layout from vex/aim.py)."""
        await self.ensure_connected()
        if len(data) > AUDIO_MAX_BYTES:
            raise AimError(f"That audio is {len(data) // 1024} KB; the robot accepts at most 255 KB.")
        if kind == "wav" and not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
            raise AimError("That isn't a valid WAV file.")
        header = bytearray(64)
        header[0] = 0 if kind == "wav" else 1
        header[1] = max(0, min(100, int(volume)))
        header[4:8] = len(data).to_bytes(4, "little")
        header[8:12] = (0).to_bytes(4, "little")  # chunk number
        encoded = name.encode("ascii", "ignore")[:32]
        header[32:32 + len(encoded)] = encoded
        try:
            await self._sockets["ws_audio"].send(bytes(header) + data)
        except ConnectionClosed:
            self._lose("the audio connection closed")
            raise AimError(self.connection_problem()) from None
        self.last_activity = time.monotonic()

    async def set_detection(self, *, apriltags: bool | None = None, ai_objects: bool | None = None,
                            colours: bool | None = None) -> None:
        if apriltags is not None:
            await self.command("tag_detection", b_enable=apriltags)
            self.apriltags_on = apriltags
        if ai_objects is not None:
            await self.command("model_detection", b_enable=ai_objects)
        if colours is not None:
            await self.command("color_detection", b_enable=colours, b_merge=True)

    async def set_colour(self, colour_id: int, rgb: tuple[int, int, int], hue_range: float, saturation_range: float) -> None:
        """Teach the onboard AI a colour signature (id 1-7), as VEXcode's AI Vision Utility does.
        hue_range is in degrees and saturation_range 0-1 (VEX's examples use 10 and 0.2)."""
        r, g, b = rgb
        await self.command("color_description", id=colour_id, red=r, green=g, blue=b,
                           hangle=hue_range, hdsat=saturation_range)

    async def fresh_status(self, timeout: float = 2.0) -> dict:
        """Wait for a status snapshot newer than the current one."""
        return await self._status.wait_newer(self._status.seq, timeout)

    # ----------------------------------------------------------------------------------
    # Camera. The stream runs while someone is watching (live view) and lingers briefly
    # after one-off captures, so consecutive looks don't restart it every time.
    # ----------------------------------------------------------------------------------

    async def _start_stream(self) -> None:
        ws = self._sockets["ws_img"]
        try:
            await _drain(ws)
            self._image_task = asyncio.create_task(self._image_loop(ws), name="aim-camera")
            await ws.send(b"\x01")
        except ConnectionClosed:
            self._lose("the camera connection closed")
            raise AimError(self.connection_problem()) from None
        self._streaming = True
        log.info("camera stream on")

    async def _stop_stream(self) -> None:
        if not self._streaming:
            return
        self._streaming = False
        log.info("camera stream off")
        ws = self._sockets.get("ws_img")
        if ws:
            try:
                await ws.send(b"\x00")
            except ConnectionClosed:
                pass
        if self._image_task:
            self._image_task.cancel()
            self._image_task = None

    async def _image_loop(self, ws: ClientConnection) -> None:
        try:
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes) and msg[:2] == b"\xff\xd8":  # JPEG start marker
                    await self._frames.put(msg)
        except ConnectionClosed:
            self._lose("the camera connection closed")

    async def camera_frame(self, timeout: float = 4.0) -> bytes:
        """A JPEG captured after this call started."""
        await self.ensure_connected()
        async with self._camera_lock:
            restarted = not self._streaming
            if restarted:
                await self._start_stream()
            # Right after a (re)start, the first frame may be one left over from earlier.
            seq = self._frames.seq + (1 if restarted else 0)
            self._stream_until = max(self._stream_until, time.monotonic() + self.STREAM_LINGER)
        self.last_activity = time.monotonic()
        try:
            return await self._frames.wait_newer(seq, timeout)
        except TimeoutError:
            raise AimError("The camera didn't send a picture in time.") from None

    async def frames(self) -> AsyncIterator[bytes]:
        """Every new frame, for as long as the caller keeps iterating (live view)."""
        await self.ensure_connected()
        async with self._camera_lock:
            self._viewers += 1
            if not self._streaming:
                await self._start_stream()
        try:
            seq = self._frames.seq
            while True:
                frame = await self._frames.wait_newer(seq, timeout=5)
                seq = self._frames.seq
                self.last_activity = time.monotonic()
                yield frame
        finally:
            self._viewers -= 1
            self._stream_until = time.monotonic() + self.STREAM_LINGER
