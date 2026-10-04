"""MCP server that lets an AI assistant drive, see through, and play with a VEX AIM robot over Wi-Fi.

Any MCP app can use it. Apps that start servers themselves (Claude, Codex, VS Code, Cursor…) run the
vex-aim-mcp command, which speaks MCP over stdio. Apps that connect to a URL (ChatGPT) need
`vex-aim-mcp --http`, which serves it over HTTP at a secret address, reached through a tunnel (see the
README). Settings (environment variables):
  AIM_HOST                 robot IP or hostname (default 192.168.4.1, the robot's own hotspot)
  AIM_MAX_SPEED_PERCENT    cap on drive and turn speed (default 60)
  AIM_MAX_MOVE_MM          cap on a single move (default 1000)
  AIM_IDLE_DISCONNECT_MIN  release the robot after this many idle minutes (default 10; 0 = never)
  AIM_LIVE_VIEW_PORT       port for the control panel web page (default 8765)
  AIM_YOLO_MODEL           Ultralytics weights for YOLO (default yolo26n.pt, downloaded on first use)
  AIM_PANEL_SETUP          where the panel saves measurements, tags, the field and abilities
                           (default: panel_setup.json in the data folder, e.g. ~/Library/Application Support/VEX AIM Panel)
  AIM_HTTP_PORT            --http: the port (default 8000)
  AIM_HTTP_SECRET          --http: the secret part of the address (default: a new random one each start)
"""

import argparse
import asyncio
import functools
import json
import logging
import math
import os
import secrets
import shutil
import sys
import tempfile
import textwrap
import time
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field
from websockets.exceptions import ConnectionClosed

from .aim_client import (AUDIO_MAX_BYTES, DRIVE_MAX_MMPS, EMOJI, FLAG_CRASHED, SOUNDS, TURN_MAX_DPS, AimError,
                        AimRobot, Detection, held_object, is_held, parse_detections, parse_note)
from . import plays
from . import routines
from . import screen
from . import strategy
from .panel import DEFAULT_YOLO_MODEL, ControlPanel
from .world import PanelState, approx_distance_mm
from .world import teach_colour as teach_colour_from_box
from .vision import YoloDetector, annotate, fmt_bearing, frame_size, onboard_boxes

log = logging.getLogger("vex_aim.server")


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


HOST = os.environ.get("AIM_HOST", "192.168.4.1")
MAX_SPEED = min(max(_env_number("AIM_MAX_SPEED_PERCENT", 60), 5), 100)
MAX_MOVE_MM = _env_number("AIM_MAX_MOVE_MM", 1000)

robot = AimRobot(HOST, idle_disconnect_s=_env_number("AIM_IDLE_DISCONNECT_MIN", 10) * 60)
yolo = YoloDetector(os.environ.get("AIM_YOLO_MODEL") or DEFAULT_YOLO_MODEL)
state = PanelState()  # team, labels, taught colours, selections: shared with the control panel
panel = ControlPanel(robot, state, int(_env_number("AIM_LIVE_VIEW_PORT", 8765)), yolo)

INSTRUCTIONS = """\
Controls a VEX AIM robot over Wi-Fi: a small classroom robot with three omni-wheels (it can drive
in any direction without turning first), a kicker for balls and barrels, a 240x240 touchscreen,
six RGB lights, a speaker, and a front camera whose onboard AI recognises sports balls, blue and
orange barrels, other AIM robots and AprilTags.

Conventions
- move direction_deg is relative to the robot's front: 0 forward, 90 right, 180 back, 270 left.
- turn degrees: positive = clockwise (right), negative = anticlockwise (left).
- heading_deg: 0-360 clockwise; 0 is the way the robot faced when it connected (or at reset_position).
- Bearings (look, detect_objects) are degrees left (-) or right (+) of straight ahead; turn(bearing)
  faces the object. The camera sees about 34 degrees either side. A bigger box usually means closer.

Fetching and kicking a ball or barrel
- scan_surroundings (or face_object) to find it, approach_object to drive it into the kicker, look()
  to confirm, then kick. The kicker holds balls and barrels with a magnet, so kick backs the robot
  away straight afterwards; otherwise a ball that rolls or bounces back gets caught again.
- Aim the kick at open space: balls bounce off obstacles. Use "soft" on a table.
- The onboard AI struggles in dim light and often loses a ball once it's right in front of the
  robot. When in doubt, trust look() over robot_status().holding.

Goal games
- A goal is two barrels of the same colour; the ball must pass between them. Ask which team the
  robot is on and call set_team; like VEX alliances, a team scores in the goal of its own colour.
- To score: approach_object the ball, look() to confirm it's held, then shoot_at_goal. It finds the
  goal, aims between the posts, kicks, backs away and returns a photo.

Control panel and real-time reactions
- control_panel opens a web page on this computer (show it in your app's built-in browser if it has
  one; otherwise give the person the link, or pass open_browser) with the live camera, team buttons,
  labelling, colour teaching, a STOP button, a map and a log; your tool calls appear in its log.
- What the person does there is shared: robot_status().panel has the team, labels, taught colours
  and the last object they pointed out. Labels (e.g. "left post") and taught colours (e.g. "red cup")
  can be used as the label argument of approach_object, face_object and wait_for.
- wait_for returns within a tenth of a second of something happening (an object appearing,
  leaving, getting close, a screen touch, a bump, or an action in the panel), so use it instead of
  polling. E.g. ask the person to point something out, then wait_for("panel").
- teach_colour makes the onboard AI detect a colour you can see in a look() photo, by its box.

Explore, teams, strategy
- explore_arena maps the arena: it scans, then drives a grid across the field chosen in the panel.
  Afterwards robot_status().panel has the map, the robot's field position, AprilTags (field markers
  locate the robot; obstacles have a keep-out zone and a distance and bearing from the robot) and
  what the person said each tag means ("meaning"). Steer clear of obstacles when planning moves.
- The person names the robot's player and picks its team in the panel (robot_status().panel.player).
- Experiments measure what the robot can do: test_kick (how far each kick strength sends the ball;
  if it rolls out of view, ask the person to measure it and call record_kick_distance) and test_speed.
  robot_status().panel.abilities has the results: use them to judge whether to pass, shoot or
  dribble (a kicked ball is usually far faster than the robot can drive).
- These run for a while; the panel's STOP (or stop) ends them.

Safety (this robot is used with children)
- Motion starts locked on every connection. Before the first move, turn or kick, ask the user to
  confirm the robot is on the floor with clear space around it, then call enable_motion. Never
  assume it is safe: robots get left on tables.
- Explore in small steps (about 300 mm or less) and look() before driving into unseen space.
- stop() halts everything immediately and is always allowed.
- Never kick toward people or fragile things.
"""


@asynccontextmanager
async def lifespan(_server):
    try:
        yield {}
    finally:
        await panel.stop()
        await yolo.close()
        await robot.disconnect()  # stops the wheels first if they're turning


ASSISTANTS = (("claude", "Claude"), ("codex", "Codex"), ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"),
              ("copilot", "Copilot"), ("visual studio code", "Copilot"), ("vscode", "Copilot"), ("cursor", "Cursor"),
              ("gemini", "Gemini"), ("windsurf", "Windsurf"))


def assistant_name(client: str) -> str:
    """What the panel calls the assistant, from the app's MCP client name (e.g. claude-code → Claude).
    An app it doesn't recognise is "your AI": its technical name (say "mcp-inspector") reads badly."""
    low = client.lower()
    return next((name for key, name in ASSISTANTS if key in low), "your AI")


class NoteClient:
    """MCP middleware: notes which app connected, from its initialize request, so the control panel can
    call the assistant by name ("Point out to ChatGPT")."""

    async def __call__(self, ctx, call_next):
        if ctx.method == "initialize" and isinstance(ctx.params, dict):
            client = str((ctx.params.get("clientInfo") or {}).get("name") or "")
            state.assistant = assistant_name(client)
            log.info("connected app: %s (%s)", state.assistant, client)
        return await call_next(ctx)


mcp = MCPServer("vex-aim", instructions=INSTRUCTIONS, lifespan=lifespan, middleware=[NoteClient()])


def brief(kwargs: dict) -> str:
    """Tool arguments for the control panel's log: rounded numbers, nothing left at its default."""
    def show(v):
        if isinstance(v, float):
            return f"{v:g}" if v == int(v) else f"{v:.1f}"
        if isinstance(v, list):
            return "[" + ", ".join(show(x) for x in v) + "]"
        return repr(v)

    text = ", ".join(f"{k}={show(v)}" for k, v in kwargs.items() if v is not None and v != "any")
    return text if len(text) <= 90 else text[:89] + "…"


def tool(title: str, *, read_only: bool = False, physical: bool = False):
    """Register a tool, turning robot problems into clean tool errors for the assistant and noting
    any automatic reconnect (which resets heading and position)."""
    annotations = ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=physical,
                                  open_world_hint=False)

    def register(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            connections_before = robot.connection_count
            call = f"{fn.__name__}({brief(kwargs)})"
            try:
                result = await fn(*args, **kwargs)
            except AimError as e:
                state.log("assistant", f"{call} failed: {e}")
                raise ToolError(str(e)) from e
            except ConnectionClosed:
                state.log("assistant", f"{call} failed: lost the connection")
                raise ToolError("Lost the connection to the robot during that command. Try again; "
                                "it reconnects automatically.") from None
            except ToolError as e:
                state.log("assistant", f"{call} refused: {e}")
                raise
            first = result[0] if isinstance(result, list) and result else result
            gist = first.splitlines()[0][:140] if isinstance(first, str) else "done"
            state.log("assistant", f"{call} → {gist}")
            if connections_before and robot.connection_count > connections_before:
                note = "(Reconnected to the robot, so heading and position were reset to 0.)"
                if isinstance(result, str):
                    result = f"{note} {result}"
                elif isinstance(result, list):
                    result = [note, *result]
                elif isinstance(result, dict):
                    result = {"note": note, **result}
            return result

        return mcp.tool(title=title, annotations=annotations, structured_output=False)(run)

    return register


COLORS = {
    "red": (255, 0, 0), "orange": (255, 133, 0), "yellow": (255, 255, 0), "green": (0, 255, 0),
    "cyan": (0, 255, 255), "blue": (0, 0, 255), "purple": (128, 0, 255), "magenta": (255, 0, 255),
    "pink": (255, 64, 160), "white": (255, 255, 255), "black": (0, 0, 0), "off": (0, 0, 0),
    "vex_blue": (0, 24, 113),
}


def parse_color(value: str) -> tuple[int, int, int]:
    v = value.strip().lower()
    if v in COLORS:
        return COLORS[v]
    hexpart = v.lstrip("#")
    if len(hexpart) == 3:
        hexpart = "".join(c * 2 for c in hexpart)
    if len(hexpart) == 6:
        try:
            return tuple(int(hexpart[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    raise ToolError(f"Unknown colour '{value}'. Use a name ({', '.join(COLORS)}) or a hex code like #FF8800.")


def speed_note(requested: float) -> str:
    return f" (capped at {MAX_SPEED:.0f}%)" if requested > MAX_SPEED else ""


def where_now() -> str:
    x, y = robot.position
    return f"Heading {round(robot.heading) % 360}°, position x={int(round(x))} y={int(round(y))} mm."


# --------------------------------------------------------------------------------------
# Seeing
# --------------------------------------------------------------------------------------

@tool("Robot status", read_only=True)
async def robot_status():
    """Battery, heading, position, tilt, whether it's moving or playing sound, what the kicker
    seems to hold, and what the robot's onboard AI vision sees right now. Connects if needed.
    "holding" relies on the onboard AI, which often misses a held ball in dim light; use look()."""
    await robot.ensure_connected()
    snap = robot.snapshot()
    snap["onboard_detections"] = [describe(d) for d in robot.detections()]
    snap["panel"] = {**state.summary((*robot.position, robot.heading)), "url": panel.url if panel.running else None}
    snap["limits"] = {"max_speed_percent": MAX_SPEED, "max_move_mm": MAX_MOVE_MM}
    return snap


def describe(d: Detection) -> dict:
    """A detection as data, with any label the person gave it and a rough distance."""
    info = d.as_dict()
    if d.kind == "color" and d.id in state.colours:
        info["name"] = state.colours[d.id]["label"]
    elif label := state.label_for(d, robot.heading):
        info["label"] = label
    if mm := approx_distance_mm(d, state):
        info["approx_distance_mm"] = mm
    return info


@tool("Look through the camera", read_only=True)
async def look(
    overlay: Annotated[bool, Field(description="Draw a bearing ruler and detection boxes on the photo")] = True,
    yolo_detection: Annotated[bool, Field(description="Also run YOLO on this computer to label 80 kinds of "
                                                      "everyday object (person, cup, bottle, chair...). "
                                                      "Takes a few seconds the first time.")] = False,
):
    """Take a photo with the robot's front camera and see it yourself. Objects the robot's onboard
    AI recognises (sports balls, blue/orange barrels, AIM robots, AprilTags) are listed with their
    bearing. For anything else, read its bearing off the ruler along the top of the photo."""
    await robot.ensure_connected()
    jpeg = await robot.camera_frame()
    heading = robot.heading
    boxes = onboard_boxes(robot.detections(), frame_size(jpeg), name_for=lambda d: state.display_name(d, heading))
    if yolo_detection:
        boxes += await yolo.detect(jpeg)
    lines = [f"{where_now()} Photo 640x480; bearings are degrees left (-) / right (+) of straight ahead."]
    if boxes:
        lines += ["Recognised:"] + [f"- {b.describe()}" for b in boxes]
    else:
        lines.append("Nothing recognised automatically" + ("" if yolo_detection else
                     " by the onboard AI (it only knows balls, barrels, AIM robots and AprilTags)") + ".")
    if overlay:
        jpeg = await asyncio.to_thread(annotate, jpeg, boxes)
    return ["\n".join(lines), Image(data=jpeg, format="jpeg")]


@tool("Detect objects", read_only=True)
async def detect_objects(
    source: Annotated[Literal["onboard", "yolo", "both"], Field(
        description="onboard: the robot's own AI (instant). yolo: YOLO on this computer (80 everyday classes).")] = "onboard",
    apriltags: Annotated[bool, Field(description="Switch on the robot's AprilTag detection first")] = False,
):
    """List what the robot can see, with bearings, without fetching a photo."""
    await robot.ensure_connected()
    if apriltags and not robot.apriltags_on:
        await robot.set_detection(apriltags=True)
        await asyncio.sleep(0.3)
    found = {}
    if source in ("onboard", "both"):
        found["onboard"] = [describe(d) for d in parse_detections(await robot.fresh_status())]
    if source in ("yolo", "both"):
        found["yolo"] = [b.as_dict() for b in await yolo.detect(await robot.camera_frame())]
    found["heading_deg"] = robot.heading
    return found


@tool("Control panel")
async def control_panel(
    enabled: Annotated[bool, Field(description="true to open the control panel, false to close it")] = True,
    yolo_detection: Annotated[bool, Field(description="Also draw YOLO detections (80 everyday kinds of "
                                                      "object); takes ~10 s to load the first time")] = False,
    open_browser: Annotated[bool, Field(description="Also open it in this computer's web browser: for apps "
                                                    "without a built-in browser")] = False,
):
    """Open (or close) the control panel: a web page on this computer with the robot's live camera and
    detection boxes, team buttons, object labelling, colour teaching, a STOP button, a top-down map and
    a log of what the person, you and the robot do. If your app has a built-in browser, show the URL
    there; otherwise give the person the link, or set open_browser. What the person chooses there is
    shared with you: see robot_status().panel and wait_for("panel")."""
    if not enabled:
        await panel.stop()
        return "Control panel closed."
    url = await panel.start(use_yolo=yolo_detection)
    if open_browser:
        import webbrowser
        await asyncio.to_thread(webbrowser.open, url)
    try:
        await robot.ensure_connected()
        note = ""
    except AimError as e:
        note = f" The robot isn't reachable yet ({e}); the panel keeps trying and connects once it's awake."
    opened = " It's open in this computer's web browser." if open_browser else (
        " Show it in your app's browser if it has one, or give the person the link.")
    return f"Control panel running at {url}.{opened}{note}"


# --------------------------------------------------------------------------------------
# Moving
# --------------------------------------------------------------------------------------

@tool("Enable motion")
async def enable_motion(
    confirmed_clear_floor: Annotated[bool, Field(
        description="true only if the user has said in this conversation that the robot is on the floor "
                    "with clear space around it")],
):
    """Unlock move, turn, turn_to_heading, face_object and kick for this connection. Motion starts
    locked every time the robot connects, because it may be sitting on a table. Ask the user first;
    never call this on your own initiative."""
    if not confirmed_clear_floor:
        raise ToolError("Motion stays locked. Ask the user to put the robot on the floor with clear space "
                        "around it, then call enable_motion with confirmed_clear_floor=true.")
    await robot.ensure_connected()
    robot.motion_enabled = True
    return (f"Motion enabled until the robot disconnects. Limits: speed {MAX_SPEED:.0f}% at most, "
            f"{MAX_MOVE_MM:.0f} mm per move.")


@tool("Move", physical=True)
async def move(
    distance_mm: Annotated[float, Field(description="How far to travel, in millimetres")],
    direction_deg: Annotated[float, Field(description="Direction relative to the robot's front: 0 forward, "
                                                      "90 right, 180 back, 270 left. It slides sideways "
                                                      "without turning.")] = 0,
    speed_percent: Annotated[float, Field(ge=5, le=100, description="100% = 200 mm/s")] = 40,
):
    """Drive a set distance in any direction, then stop. Waits until it has finished. Stops early if
    the robot bumps into something."""
    if abs(distance_mm) > MAX_MOVE_MM:
        raise ToolError(f"Moves are limited to {MAX_MOVE_MM:.0f} mm at a time. Split it into shorter moves "
                        "and look() in between.")
    await robot.ensure_connected()
    require_motion()  # refuses while the person is driving (Driver mode), or motion is locked
    speed = min(speed_percent, MAX_SPEED)
    outcome = await robot.move(distance_mm, direction_deg % 360, speed / 100 * DRIVE_MAX_MMPS)
    return f"Move {outcome}{speed_note(speed_percent)}. {where_now()}"


@tool("Turn", physical=True)
async def turn(
    degrees: Annotated[float, Field(ge=-720, le=720, description="Positive = clockwise (right), "
                                                                "negative = anticlockwise (left)")],
    speed_percent: Annotated[float, Field(ge=5, le=100, description="100% = 180°/s")] = 40,
):
    """Spin on the spot by an angle, then stop. To face something seen in look(), turn by its bearing."""
    await robot.ensure_connected()
    require_motion()  # refuses while the person is driving (Driver mode), or motion is locked
    speed = min(speed_percent, MAX_SPEED)
    outcome = await robot.turn(degrees, speed / 100 * TURN_MAX_DPS)
    return f"Turn {outcome}{speed_note(speed_percent)}. {where_now()}"


@tool("Turn to heading", physical=True)
async def turn_to_heading(
    heading_deg: Annotated[float, Field(ge=0, lt=360, description="0 = the way the robot faced when it "
                                                                  "connected (or at reset_position), clockwise")],
    speed_percent: Annotated[float, Field(ge=5, le=100, description="100% = 180°/s")] = 40,
):
    """Spin on the spot to face an absolute heading."""
    await robot.ensure_connected()
    require_motion()  # refuses while the person is driving (Driver mode), or motion is locked
    speed = min(speed_percent, MAX_SPEED)
    outcome = await robot.turn_to(heading_deg, speed / 100 * TURN_MAX_DPS)
    return f"Turn {outcome}{speed_note(speed_percent)}. {where_now()}"


TARGET_NAMES = {"sports_ball": "SportsBall", "blue_barrel": "BlueBarrel", "orange_barrel": "OrangeBarrel",
                "aim_robot": "Robot"}


def require_motion() -> None:
    if state.mode == "driver":
        raise ToolError("The person is driving the robot (Driver mode in the control panel). Ask them to switch to "
                        "Auto before you move it.")
    if not robot.motion_enabled:
        raise ToolError("Motion is locked. Ask the user to confirm the robot is on the floor with clear "
                        "space around it, then call enable_motion.")


def turn_speed(percent: float) -> float:
    return min(percent, MAX_SPEED) / 100 * TURN_MAX_DPS


def drive_speed(percent: float) -> float:
    return min(percent, MAX_SPEED) / 100 * DRIVE_MAX_MMPS


LABEL_FIELD = Field(description="Instead of target: the label of an object the person named in the control "
                                "panel (e.g. 'left post'), or a taught colour (e.g. 'red cup')")


def matching(dets: list[Detection], target: str, label: str | None = None) -> list[Detection]:
    if label:
        wanted = label.strip().lower()
        return [d for d in dets if (state.label_for(d, robot.heading) or "").lower() == wanted]
    if target == "apriltag":
        return [d for d in dets if d.kind == "apriltag"]
    if target == "any":
        return dets
    return [d for d in dets if d.name == TARGET_NAMES[target]]


def check_label(label: str | None) -> None:
    known = [e["label"] for e in state.labels] + [c["label"] for c in state.colours.values()]
    if label and label.strip().lower() not in (k.lower() for k in known):
        raise ToolError(f"No object or colour is labelled '{label}'. Known labels: "
                        f"{', '.join(known) or 'none yet (label things in the control panel)'}.")


async def seen(target: str, tries: int = 1, label: str | None = None) -> list[Detection]:
    """Onboard detections of target, largest first, once the vision has caught up with the last
    movement. Retries, because the onboard AI sometimes misses an object for a frame or two."""
    if target == "apriltag" and not robot.apriltags_on:
        await robot.set_detection(apriltags=True)
    for _ in range(tries):
        await asyncio.sleep(0.4)
        found = matching(parse_detections(await robot.fresh_status()), target, label)
        if found:
            return found
    return []


@tool("Face an object", physical=True)
async def face_object(
    target: Literal["sports_ball", "blue_barrel", "orange_barrel", "aim_robot", "apriltag", "any"] = "any",
    label: Annotated[str | None, LABEL_FIELD] = None,
    search: Annotated[bool, Field(description="If it's not in view, turn in 45° steps (up to a full "
                                              "circle) looking for it")] = True,
    speed_percent: Annotated[float, Field(ge=5, le=100)] = 40,
):
    """Find an object with the robot's onboard AI vision and turn to face it. Works for what the
    onboard AI knows (sports balls, blue and orange barrels, other AIM robots, AprilTags), labelled
    objects, and colours taught with teach_colour or the control panel."""
    await robot.ensure_connected()
    require_motion()
    check_label(label)
    dps = turn_speed(speed_percent)
    searched = 0
    what = f"“{label}”" if label else target.replace("_", " ")
    while True:
        found = await seen(target, label=label)
        if found:
            for _ in range(2):  # turn, then correct once
                if abs(found[0].bearing) <= 3:
                    break
                await robot.turn(found[0].bearing, dps)
                found = await seen(target, label=label) or found
            return (f"Facing {state.display_name(found[0], robot.heading)}, {fmt_bearing(found[0].bearing)} off "
                    f"centre ({found[0].width}×{found[0].height} px in the robot's 320×240 view). {where_now()}")
        if not search or searched >= 360:
            return f"No {what} in view" + (f" after turning {searched}°." if searched else ".")
        await robot.turn(45, dps)
        searched += 45


def wrap180(angle: float) -> float:
    return (angle + 180) % 360 - 180


def merge_sightings(sightings: list[tuple[float, Detection]]) -> list[dict]:
    """Combine sightings (absolute heading, detection) of the same object seen from different turns:
    same kind within 5° of heading. Keeps the most central view's heading (bearings are most accurate
    near the middle of the picture) and the largest size seen."""
    objects: list[dict] = []
    for heading, d in sightings:
        for o in objects:
            if o["name"] == d.name and abs(wrap180(heading - o["heading"])) < 5:
                if abs(d.bearing) < abs(o["bearing"]):
                    o["heading"], o["bearing"] = heading, d.bearing
                o["width"], o["height"] = max(o["width"], d.width), max(o["height"], d.height)
                break
        else:
            objects.append({"name": d.name, "heading": heading, "bearing": d.bearing,
                            "width": d.width, "height": d.height})
    return objects


async def survey(dps: float) -> tuple[list[dict], str | None]:
    """Turn a full circle in 45° steps (the camera sees ±34°, so views overlap), recording every
    onboard detection, then face the starting direction again. Returns the objects around the robot
    and what's in the kicker: a held object turns with the robot, so it isn't mapped."""
    start, sightings, holding = robot.heading, [], None
    for step in range(8):
        for d in await seen("any"):
            if is_held(d):
                holding = d.name
            else:
                sightings.append(((robot.heading + d.bearing) % 360, d))
        if step < 7:
            await robot.turn(45, dps)
    await robot.turn_to(start, dps)
    return merge_sightings(sightings), holding


GOAL_POSTS = {"blue": "BlueBarrel", "orange": "OrangeBarrel"}


def find_goal(objects: list[dict], colour: str) -> dict | None:
    """A goal is the two biggest (nearest) barrels of one colour. Its centre is the midpoint between
    them, placing each post by its heading at a distance proportional to 1/width: the unknown scale
    cancels out of the direction."""
    posts = sorted((o for o in objects if o["name"] == GOAL_POSTS[colour]), key=lambda o: -o["width"])[:2]
    if len(posts) < 2:
        return None
    x = sum(math.sin(math.radians(o["heading"])) / max(o["width"], 1) for o in posts)
    y = sum(math.cos(math.radians(o["heading"])) / max(o["width"], 1) for o in posts)
    return {"colour": colour, "heading": math.degrees(math.atan2(x, y)) % 360,
            "posts": sorted(round(o["heading"]) % 360 for o in posts),
            "gap_deg": abs(wrap180(posts[0]["heading"] - posts[1]["heading"]))}


async def goal_in_view(colour: str) -> dict | None:
    """The goal measured from the current picture alone, if both posts are in it."""
    await asyncio.sleep(0.4)
    dets = [d for d in parse_detections(await robot.fresh_status()) if not is_held(d)]  # a held barrel isn't a post
    return find_goal(merge_sightings([((robot.heading + d.bearing) % 360, d) for d in dets]), colour)


@tool("Scan surroundings", physical=True)
async def scan_surroundings(speed_percent: Annotated[float, Field(ge=5, le=100)] = 30):
    """Turn a full circle on the spot, noting the heading of every object the onboard AI recognises
    (sports balls, barrels, AIM robots, and AprilTags once switched on) and where each goal (a pair
    of same-coloured barrels) is centred, then face the starting direction again. Use the headings
    with turn_to_heading, approach_object or shoot_at_goal."""
    await robot.ensure_connected()
    require_motion()
    objects, holding = await survey(turn_speed(speed_percent))
    held = f"\nIn the kicker: {holding}." if holding else ""
    if not objects:
        return f"Turned a full circle and the onboard AI recognised nothing around the robot.{held} {where_now()}"
    def name(o: dict) -> str:
        label = state.label_at(o["name"], o["heading"])
        return f"{o['name']} “{label}”" if label else o["name"]

    lines = [f"- {name(o)}: heading {round(o['heading']) % 360}°, {o['width']}×{o['height']} px box "
             "(bigger = closer)" for o in sorted(objects, key=lambda o: o["heading"])]
    for colour in GOAL_POSTS:
        if goal := find_goal(objects, colour):
            lines.append(f"- {colour} goal: centre at heading {round(goal['heading']) % 360}°, posts at "
                         f"{goal['posts'][0]}° and {goal['posts'][1]}° ({goal['gap_deg']:.0f}° apart)")
    return "Objects around the robot:\n" + "\n".join(lines) + held + f"\n{where_now()}"


@tool("Set team")
async def set_team(colour: Literal["blue", "orange"]):
    """Choose the robot's team at the start of a goal game. Like VEX alliances, a team scores in the
    goal of its own colour: between the two barrels of that colour. (The person can also pick the
    team in the control panel.)"""
    panel.fleet.pick().team = colour  # the selected player's team; the panel's team cards follow
    panel._sync_player()
    return f"The robot is on the {colour} team: shoot_at_goal will aim between the two {colour} barrels."


@tool("Shoot at goal", physical=True)
async def shoot_at_goal(
    goal: Annotated[Literal["team", "blue", "orange"], Field(
        description='"team" means the goal of the colour chosen with set_team')] = "team",
    strength: Annotated[Literal["soft", "medium", "hard"], Field(description='Use "soft" on a table')] = "soft",
    speed_percent: Annotated[float, Field(ge=5, le=100)] = 30,
):
    """Score with the ball the robot is holding: find the goal (two barrels of one colour), turn so
    the ball passes between them, kick, and back away. Get the ball into the kicker first
    (approach_object) and confirm with look(). Returns a photo taken after the kick."""
    await robot.ensure_connected()
    require_motion()
    colour = state.team if goal == "team" else goal
    if colour is None:
        raise ToolError("No team chosen yet. Ask the user which team the robot is on and call set_team, "
                        "or name the goal's colour.")
    dps = turn_speed(speed_percent)
    aim = await goal_in_view(colour)
    if aim is None:
        aim = find_goal((await survey(dps))[0], colour)
        if aim is None:
            raise ToolError(f"Couldn't find two {colour} barrels around the robot. Is the goal set up and "
                            "well lit? (scan_surroundings shows what the robot can see.)")
    for _ in range(3):  # turn to the centre, re-measuring from there while both posts are in view
        off = wrap180(aim["heading"] - robot.heading)
        if abs(off) <= 2:
            break
        await robot.turn(off, dps)
        aim = await goal_in_view(colour) or aim
    await robot.kick(strength)
    back = min(80.0, MAX_MOVE_MM)
    await robot.move(back, 180, drive_speed(50))
    await asyncio.sleep(1.0)  # let the ball finish rolling
    return await report_with_photo(
        f"Aimed at the centre of the {colour} goal (heading {round(aim['heading']) % 360}°, between posts at "
        f"{aim['posts'][0]}° and {aim['posts'][1]}°), kicked ({strength}) and backed away {back:.0f} mm. "
        f"{where_now()} Check the photo to see whether it went in.")


# Distance to a sports ball from its width in the onboard AI's 320-px-wide view, and how far in
# front of the camera it sits once in the kicker. Calibrated on a real robot (2026-10-03): the
# onboard AI (and YOLO) stop recognising the ball in the last ~10 cm, so the end is estimated.
BALL_SIZE_K = 8700  # ≈ camera focal length (px) × ball diameter (mm)
KICKER_MM = 50


async def report_with_photo(text: str) -> list:
    """For uncertain outcomes: the result text plus a fresh annotated photo for the assistant to check."""
    jpeg = await robot.camera_frame()
    heading = robot.heading
    boxes = onboard_boxes(robot.detections(), frame_size(jpeg), name_for=lambda d: state.display_name(d, heading))
    jpeg = await asyncio.to_thread(annotate, jpeg, boxes)
    return [text, Image(data=jpeg, format="jpeg")]


@tool("Approach an object", physical=True)
async def approach_object(
    target: Literal["sports_ball", "blue_barrel", "orange_barrel", "aim_robot", "apriltag", "any"] = "any",
    label: Annotated[str | None, LABEL_FIELD] = None,
    max_distance_mm: Annotated[float, Field(gt=0, le=2000, description="Give up after driving this far in total "
                                                                       "(also limited by the server's move cap)")] = 400,
    speed_percent: Annotated[float, Field(ge=5, le=100)] = 30,
):
    """Drive up to an object the onboard AI can see, re-aiming before every short step, until it's
    in the kicker (whose magnet then holds balls and barrels). Give a target kind or the label of
    an object or taught colour. If it isn't in view, use face_object or scan_surroundings first.
    The onboard AI often loses a ball in the last ~10 cm, so the robot finishes that part on an
    estimate. Unless the AI confirms the object is in the kicker, the result includes a photo:
    check it before kicking."""
    await robot.ensure_connected()
    require_motion()
    check_label(label)
    if target == "any" and not label:
        raise ToolError("Say what to approach: a target kind (e.g. sports_ball) or a label.")
    budget = min(max_distance_mm, MAX_MOVE_MM)
    dps, mmps = turn_speed(speed_percent), drive_speed(speed_percent)
    travelled, seen_at, last, steps = 0.0, 0.0, None, []

    def report(result: str) -> str:
        return (f"{result}, after driving {travelled:.0f} mm. {where_now()}"
                + (f"\nSteps: {'; '.join(steps)}." if steps else ""))

    for _ in range(40):
        found = await seen(target, tries=3, label=label)
        if not found:
            if last is None:
                what = f"“{label}”" if label else target.replace("_", " ")
                raise ToolError(f"No {what} in view. Use face_object or scan_surroundings first.")
            bottom = last.y + last.height
            if abs(last.bearing) < 8 and last.name == "SportsBall":
                # Estimate what's left from how big the ball looked when last seen.
                remaining = BALL_SIZE_K / max(last.width, 1) - KICKER_MM - (travelled - seen_at)
                nudge = min(max(remaining + 20, 0.0), 120.0, budget - travelled)
            elif abs(last.bearing) < 10 and bottom >= 120:  # it was low and centred: now just below the view
                nudge = min(60.0 if bottom < 150 else 40.0, budget - travelled)
            else:
                return await report_with_photo(report(f"Lost sight of the {state.display_name(last, robot.heading)}"))
            if nudge >= 10:
                outcome = await robot.move(nudge, 0, mmps)
                travelled += nudge
                steps.append(f"final {nudge:.0f} mm on an estimate (onboard AI lost it)")
                if outcome != "completed":
                    return await report_with_photo(report(f"Final move {outcome}"))
            return await report_with_photo(report(f"The {state.display_name(last, robot.heading)} should now be in "
                                                  "the kicker (the onboard AI can't see it this close). Check the photo"))
        d = last = found[0]
        seen_at = travelled
        if is_held(d):
            return report(f"The {state.display_name(d, robot.heading)} is in the kicker")
        if abs(d.bearing) > 4:
            outcome = await robot.turn(d.bearing, dps)
            steps.append(f"turn {fmt_bearing(d.bearing)}")
            if outcome != "completed":
                return report(f"Turn {outcome}")
            continue
        bottom = d.y + d.height  # lower in the view = closer, so take smaller steps
        step = min(60.0 if bottom < 120 else 45.0 if bottom < 150 else 30.0, budget - travelled)
        if step < 10:
            return await report_with_photo(
                report(f"Stopped at the {budget:.0f} mm limit with the {d.name} {fmt_bearing(d.bearing)} ahead"))
        outcome = await robot.move(step, 0, mmps)
        travelled += step
        steps.append(f"forward {step:.0f} mm")
        if outcome != "completed":
            return report(f"Move {outcome}")
    return report("Gave up after 40 steps")


@tool("Stop", physical=True)
async def stop():
    """Stop all motion immediately, including an exploration or experiment in progress. Always
    allowed, even while motion is locked."""
    ended = routines.cancel(state)
    await robot.ensure_connected()
    await robot.stop()
    return f"Stopped{' (and ended what the robot was doing by itself)' if ended else ''}. {where_now()}"


@tool("Kick", physical=True)
async def kick(
    strength: Literal["soft", "medium", "hard"] = "medium",
    back_away_mm: Annotated[float, Field(ge=0, le=200, description="Reverse this far straight after kicking, so the "
                                                                    "kicker's magnet doesn't catch a ball that rolls "
                                                                    "or bounces back (0 = stay put)")] = 80,
):
    """Fire the kicker at whatever it's holding ("soft" gently pushes or places it), then back away.
    Aim at open space first, since balls bounce off things, and make sure no one is in the way.
    Use look() to check something is in the kicker."""
    await robot.ensure_connected()
    require_motion()  # refuses while the person is driving (Driver mode), or motion is locked
    await robot.kick(strength)
    result = f"Kicked ({strength})."
    back = min(back_away_mm, MAX_MOVE_MM)
    if back >= 5:
        outcome = await robot.move(back, 180, drive_speed(50))
        result += f" Backed away {back:.0f} mm ({outcome})."
    return f"{result} {where_now()}"


# --------------------------------------------------------------------------------------
# Mapping the arena, and experiments for strategy
# --------------------------------------------------------------------------------------

@tool("Explore the arena", physical=True)
async def explore_arena(speed_percent: Annotated[float, Field(ge=5, le=100)] = 30):
    """Map the arena by driving around it. It scans a full circle, then visits spots across the field
    chosen in the control panel (a grid 30 cm in from the edges), scanning at each. It drives around
    obstacles and anything already on the map (skipping a spot only if there's no way round), and stops on a bump. Without a field it
    only scans where it is. Takes a few minutes; the person can press STOP in the panel. Afterwards,
    robot_status().panel.map lists what it found."""
    await robot.ensure_connected()
    require_motion()
    if not panel.running:  # the panel builds the map while it runs
        await panel.start(use_yolo=False)
    try:
        text = await routines.run(state, "exploration", routines.explore_field(robot, state, speed_percent))
    except AimError as e:
        raise ToolError(f"{e} {routines.map_summary(state)} {where_now()}") from None
    return f"{text} {where_now()}"


@tool("Test a kick", physical=True)
async def test_kick(
    strength: Literal["soft", "medium", "hard"] = "soft",
    ball_in_kicker: Annotated[bool, Field(description="The person confirmed the ball is in the kicker (the AI "
                                                      "often can't see a ball that close)")] = False,
):
    """Experiment: kick the ball in the kicker straight ahead and watch it roll, to measure how far
    this strength sends it. Point the robot at open floor first. The distance is saved when the ball
    stops in view. If it rolls out of the camera's range, ask the person to measure where it stopped
    and call record_kick_distance. Results build up in robot_status().panel.abilities."""
    await robot.ensure_connected()
    require_motion()
    r = await routines.run(state, "kick test", routines.kick_test(robot, state, strength, ball_in_kicker))
    speed = f", leaving the kicker at about {r['launch_mm_s'] / 10:.0f} cm/s" if r["launch_mm_s"] else ""
    if r["stopped_mm"]:
        reach = state.kick_reach()[strength]
        return (f"{strength.capitalize()} kick: the ball stopped about {r['stopped_mm'] / 10:.0f} cm from the kicker{speed}. "
                f"{strength.capitalize()} kicks average {reach['mm'] / 10:.0f} cm over {reach['tests']} test(s). {where_now()}")
    seen = (f"the ball rolled out of the camera's view at about {r['last_seen_mm'] / 10:.0f} cm, still moving{speed}"
            if r["seen"] else "the camera didn't see the ball after the kick")
    return (f"{strength.capitalize()} kick: {seen}. Ask the person to measure how far from the kicker it stopped, "
            f"then call record_kick_distance. {where_now()}")


@tool("Record a kick distance")
async def record_kick_distance(
    strength: Literal["soft", "medium", "hard"],
    distance_cm: Annotated[float, Field(gt=2, le=2000, description="How far the ball went from the kicker, as measured")],
):
    """Save a kick distance someone measured (e.g. after test_kick lost sight of the ball)."""
    try:
        routines.record_kick(state, strength, distance_cm * 10)
    except ValueError as e:
        raise ToolError(str(e)) from None
    reach = state.kick_reach()[strength]
    return f"Saved. {strength.capitalize()} kicks now average {reach['mm'] / 10:.0f} cm over {reach['tests']} test(s)."


@tool("Test driving speed", physical=True)
async def test_speed(
    speed_percent: Annotated[float, Field(ge=5, le=100)] = 30,
    distance_mm: Annotated[float, Field(ge=100, le=2000, description="Clear floor needed straight ahead")] = 500,
    return_to_start: bool = True,
):
    """Experiment: drive straight ahead a measured distance and time it, to find the robot's top speed
    at this speed setting and how quickly it gets up to speed. Needs that much clear floor ahead."""
    await robot.ensure_connected()
    require_motion()
    r = await routines.run(state, "speed test", routines.speed_test(robot, state, speed_percent,
                                                                     min(distance_mm, MAX_MOVE_MM), return_to_start))
    accel = f", reached in {r['accel_s']} s" if r["accel_s"] is not None else ""
    return (f"At {r['speed_percent']:.0f}% it drove {r['distance_mm'] / 10:.0f} cm in {r['time_s']} s: top speed "
            f"{r['top_mm_s'] / 10:.0f} cm/s (asked for {r['asked_mm_s'] / 10:.0f}){accel}, average {r['avg_mm_s'] / 10:.0f} cm/s."
            + (" It drove back to the start." if return_to_start else "") + f" {where_now()}")


# --------------------------------------------------------------------------------------
# Plays: soccer moves the robot makes by itself (plays.py), and advice (strategy.py)
# --------------------------------------------------------------------------------------

async def _play(name: str, job) -> str:
    try:
        if not panel.running:  # the panel builds the map the plays plan with
            await panel.start(use_yolo=False)
        return f"{await routines.run(state, name, job)} {where_now()}"
    except AimError as e:
        raise ToolError(f"{e} {where_now()}") from None
    finally:
        job.close()  # if it never started; harmless once it has run


@tool("Fetch the ball", physical=True)
async def fetch_ball(speed_percent: Annotated[float, Field(ge=5, le=100)] = 40):
    """Get the ball into the kicker by itself: find it (in view, on the control panel's map, or by turning to
    look around), drive to just short of it around obstacles and anything on the map, then creep up on it with
    the camera. Unlike approach_object, the ball needn't be in view. The onboard AI often loses the ball in the
    last ~10 cm; if the result says it can't confirm the ball is held, check with look(). STOP ends it."""
    await robot.ensure_connected()
    require_motion()
    return await _play("fetching the ball", plays.fetch_ball(robot, state, speed_percent))


@tool("Score a goal", physical=True)
async def score_goal(goal: Literal["team", "blue", "orange"] = "team",
                     speed_percent: Annotated[float, Field(ge=5, le=100)] = 40):
    """Score by itself from anywhere: fetch the ball if the camera sees it elsewhere, find the goal (two barrels
    of the team's colour, from the map or by looking around), dribble closer if no measured kick would score
    from here, line up between the posts, kick with the gentlest measured strength that scores, and back away.
    Only uses kick strengths measured with test_kick; with none, it dribbles to 30 cm and kicks soft.
    shoot_at_goal instead kicks from where the robot stands. Make sure nobody is near the goal."""
    await robot.ensure_connected()
    require_motion()
    return await _play("scoring", plays.shoot(robot, state, speed_percent, None if goal == "team" else goal))


@tool("Pass the ball to a spot", physical=True)
async def pass_ball(x_mm: float, y_mm: float, speed_percent: Annotated[float, Field(ge=5, le=100)] = 40):
    """Kick the ball to a spot on the field (mm, as in robot_status().panel.map: on a field (0, 0) is its centre,
    x right, y up the field) with the gentlest measured kick that gets there, fetching it first if needed and
    dribbling closer if no measured kick reaches. Refuses if something on the map is in the ball's way."""
    await robot.ensure_connected()
    require_motion()
    return await _play("passing", plays.pass_to(robot, state, x_mm, y_mm, speed_percent))


@tool("Go to a spot", physical=True)
async def go_to(x_mm: float, y_mm: float, heading_deg: float | None = None,
                speed_percent: Annotated[float, Field(ge=5, le=100)] = 40):
    """Drive to a spot (field mm when a field is chosen in the control panel, otherwise from where the robot
    connected) around obstacles and anything on the map, finishing within a few cm; optionally face
    heading_deg (0 = up the field) at the end. Refuses spots off the field or within 10 cm of its walls."""
    await robot.ensure_connected()
    require_motion()
    return await _play("going to a spot", plays.go_to(robot, state, x_mm, y_mm, speed_percent, heading_deg))


@tool("Guard the goal", physical=True)
async def guard_goal(seconds: Annotated[float, Field(gt=0, le=120)] = 30,
                     goal: Literal["defend", "blue", "orange"] = "defend",
                     speed_percent: Annotated[float, Field(ge=5, le=100)] = 40):
    """Play goalkeeper for a while: stand 25 cm in front of the goal the other team scores in (each team scores
    in its own colour's goal, so the blue team guards the orange goal), facing up the field, and slide sideways
    to stay between the ball and the goal's centre, never past a post. Ends early if it catches the ball."""
    await robot.ensure_connected()
    require_motion()
    return await _play("guarding the goal", plays.guard_goal(robot, state, seconds, None if goal == "defend" else goal, speed_percent))


def _follow_panel(selected: AimRobot) -> None:
    """When the person shows another player's robot in the control panel, the tools follow it."""
    global robot
    robot = selected


panel.on_select = _follow_panel


@tool("Team list", read_only=True)
async def team_list():
    """The robots on the team list: each player's name, team, robot address, whether it's connected, battery,
    and where it is on the field. "selected" is the one the tools and the control panel act on; use
    select_player to switch. Also which other robots the selected one can see, and whether each is a
    teammate, an opponent or unknown."""
    out = {"selected": panel.fleet.selected, "players": panel.fleet.statuses()}
    with contextlib.suppress(AimError):
        out["robots_in_view"] = [s.as_dict() for s in panel.fleet.robots_in_view(panel.fleet.pick(), state)]
    return json.dumps(out, indent=1)


@tool("Choose which robot to control")
async def select_player(name: str):
    """Make another player's robot the one the tools (and the control panel) act on. Motion starts locked on
    each robot: ask the person to confirm that robot is on the floor before enable_motion."""
    try:
        await panel._select(name)
    except AimError as e:
        raise ToolError(str(e)) from None
    return f"Now controlling {name}'s robot at {robot.host}. {where_now()}"


@tool("Add a robot to the team list")
async def add_player(name: Annotated[str, Field(max_length=16)], host: str,
                     team: Literal["blue", "orange"] | None = None):
    """Put another AIM robot on the team list, by its player name and IP address (the robot shows its IP on its
    screen under Settings → Radio; the control panel's Network menu can find robots too)."""
    try:
        player = panel.fleet.add(name, host, team)
    except AimError as e:
        raise ToolError(str(e)) from None
    panel._sync_player()
    state.log("assistant", f"added {player.name} ({player.host}) to the team list")
    return f"Added {player.name} ({player.host}, {team or 'no'} team). Use team_list to see it, select_player to control it."


@tool("What should the robot do?", read_only=True)
async def advise(holding: bool | None = None, teammates_mm: list[list[float]] | None = None,
                 speed_percent: float | None = None):
    """Strategy advice without moving: fetch, shoot, pass or dribble, and why, with the numbers behind it
    (distance to the goal, how far each measured kick rolls, how long the ball takes to roll there versus
    driving, whether the path is clear, where to dribble to). holding defaults to what the onboard AI sees,
    which often misses a held ball, so confirm with look(). teammates_mm: [[x, y], …] of teammates to pass to."""
    await robot.ensure_connected()
    if holding is None:
        holding = held_object(robot.detections()) == "SportsBall"
    try:
        advice = strategy.decide(state, (*state.on_field(*robot.position), robot.heading), holding,
                                 teammates=[tuple(t) for t in teammates_mm or []], speed_percent=speed_percent)
    except AimError as e:
        raise ToolError(str(e)) from None
    return json.dumps(advice, indent=1)


@tool("Wait for something to happen", read_only=True)
async def wait_for(
    event: Annotated[Literal["seen", "gone", "close", "touched", "bump", "panel"], Field(
        description="seen: the target comes into view. gone: it leaves the view. close: it's right in front of "
                    "the robot or in the kicker. touched: someone touches the robot's screen. bump: the robot is "
                    "knocked or crashes. panel: the person does something in the control panel.")],
    target: Literal["sports_ball", "blue_barrel", "orange_barrel", "aim_robot", "apriltag", "any"] = "any",
    label: Annotated[str | None, LABEL_FIELD] = None,
    timeout_s: Annotated[float, Field(gt=0, le=300, description="Give up after this many seconds")] = 30,
):
    """Watch the robot about 10 times a second and return the moment something happens, instead of
    checking over and over. Returns what happened (with bearings), or that it timed out."""
    await robot.ensure_connected()
    check_label(label)
    what = f"“{label}”" if label else ("anything" if target == "any" else "a " + target.replace("_", " "))
    if event == "panel":
        act = await state.wait_action(state.event_seq, timeout_s)
        if act is None:
            return f"Nothing happened in the control panel in {timeout_s:.0f} s."
        details = {k: v for k, v in act.items() if k not in ("seq", "text")}
        return f"In the control panel, the person {act['text']}.\nDetails: {json.dumps(details, default=str)}"
    if target == "apriltag" and not robot.apriltags_on:
        await robot.set_detection(apriltags=True)
    deadline = time.monotonic() + timeout_s
    crashed_before = bool(robot.flags & FLAG_CRASHED)
    while time.monotonic() < deadline:
        try:
            status = await robot.fresh_status(timeout=min(2.0, max(0.1, deadline - time.monotonic())))
        except TimeoutError:
            continue
        r = status["robot"]
        found = matching(parse_detections(status), target, label)
        if event == "seen" and found:
            d = found[0]
            return (f"Saw {state.display_name(d, robot.heading)} at {fmt_bearing(d.bearing)} "
                    f"({d.width}×{d.height} px). {where_now()}")
        if event == "gone" and not found:
            return f"{what.capitalize()} is no longer in view. {where_now()}"
        if event == "close" and (near := [d for d in found if is_held(d) or d.y + d.height >= 170]):
            held = " (in the kicker)" if is_held(near[0]) else ""
            return f"{state.display_name(near[0], robot.heading)} is right in front of the robot{held}. {where_now()}"
        if event == "touched" and int(r.get("touch_flags", "0"), 16) & 1:
            return f"Someone touched the robot's screen at ({r.get('touch_x')}, {r.get('touch_y')})."
        if event == "bump":
            crashed = bool(int(r["flags"], 16) & FLAG_CRASHED)
            if crashed and not crashed_before:
                return f"Bump detected. {where_now()}"
            crashed_before = crashed
    outcome = {"seen": f"without seeing {what}", "gone": f"and {what} is still in view",
               "close": f"and {what} never came close", "touched": "and nobody touched the screen",
               "bump": "without a bump"}[event]
    return f"Waited {timeout_s:.0f} s {outcome}."


@tool("Teach a colour")
async def teach_colour(
    label: Annotated[str, Field(description="A name for it, e.g. 'red cup'")],
    box_xyxy: Annotated[list[float], Field(min_length=4, max_length=4, description=(
        "Where it is in a look() photo, as [x0, y0, x1, y1] pixels of the 640×480 image. Choose a box well "
        "inside the object so only its colour is sampled."))],
    tolerance: Literal["tight", "normal", "loose"] = "normal",
    width_mm: Annotated[float | None, Field(gt=0, le=2000, description=(
        "The object's real width in mm, if known: lets its distance be estimated and the control panel "
        "map place it"))] = None,
):
    """Teach the robot's onboard AI to detect a colour, sampled from part of the current camera
    view (for example a cup you spotted in a look() photo). The robot then tracks it by itself,
    many times a second, and its name works as a label in approach_object, face_object and
    wait_for. This is the colour signature from VEXcode's AI Vision Utility; the robot holds 7."""
    await robot.ensure_connected()
    taught = await teach_colour_from_box(robot, state, label, box_xyxy, tolerance, width_mm)
    await asyncio.sleep(0.5)
    dets = [d for d in parse_detections(await robot.fresh_status()) if d.kind == "color" and d.id == taught["id"]]
    now = (f" The robot can see it now, at {fmt_bearing(dets[0].bearing)}." if dets else
           " The robot doesn't detect it yet: try tolerance 'loose', better light, or a box well inside the object.")
    return f"Taught “{taught['label']}” (rgb {tuple(taught['rgb'])}, colour slot {taught['id']}).{now}"


@tool("Reset position")
async def reset_position():
    """Make the current heading 0° and the current position (0, 0)."""
    await robot.ensure_connected()
    robot.reset_origin()
    return f"Reset. {where_now()}"


# --------------------------------------------------------------------------------------
# Lights, screen and sound
# --------------------------------------------------------------------------------------

@tool("Set lights")
async def set_lights(
    color: Annotated[str, Field(description="A colour name (red, orange, yellow, green, cyan, blue, purple, "
                                            "magenta, pink, white, off) or hex like #FF8800")] = "green",
    led: Annotated[Literal["all", "1", "2", "3", "4", "5", "6"], Field(
        description="Which light: 1 front-left, 2 left, 3 back-left, 4 back-right, 5 right, 6 front-right")] = "all",
):
    """Set the colour of the robot's ring of six lights."""
    rgb = parse_color(color)
    await robot.ensure_connected()
    await robot.set_led("all" if led == "all" else f"light{led}", rgb)
    return f"Light {led} set to {color}."


@tool("Show emoji")
async def show_emoji(
    emoji: Literal[EMOJI + ("none",)] = "happy",  # type: ignore[valid-type]
    look: Literal["forward", "left", "right"] = "forward",
):
    """Show one of the robot's animated faces on its screen ("none" hides it)."""
    await robot.ensure_connected()
    if emoji == "none":
        await robot.hide_emoji()
        return "Emoji hidden."
    await robot.show_emoji(emoji, look)
    return f"Showing '{emoji}' looking {look}."


FONT_LAYOUT = {  # font, characters per line, line height (estimates for the 240x240 screen)
    "small": ("mono20", 18, 24), "medium": ("mono30", 12, 34), "large": ("mono40", 9, 44), "huge": ("mono60", 6, 64),
}


@tool("Show text")
async def show_text(
    text: Annotated[str, Field(description="What to write; keep it short")],
    color: str = "white",
    background: str = "black",
    size: Literal["small", "medium", "large", "huge"] = "medium",
):
    """Write text on the robot's 240x240 screen, replacing whatever was there. Long lines wrap."""
    font, per_line, line_height = FONT_LAYOUT[size]
    lines = [w for para in text.splitlines() or [""] for w in (textwrap.wrap(para, per_line) or [""])]
    fits = (240 - 8) // line_height
    if len(lines) > fits:
        raise ToolError(f"That needs {len(lines)} lines at size '{size}' but only {fits} fit. "
                        "Shorten it or use a smaller size.")
    fg, bg = parse_color(color), parse_color(background)
    await robot.ensure_connected()
    await robot.show_text(lines, font, fg, bg, line_height)
    return f"Showing {len(lines)} line(s) of text."


@tool("React on the screen")
async def react(
    expression: Literal["happy", "excited", "sad", "wink", "surprised", "focused"] = "happy",
    caption: Annotated[str | None, Field(max_length=14, description='Short text above the face; by default "GOAL!" for '
                                                                    'excited, "So close!" for sad, "Nice pass!" for wink')] = None,
    hold_s: Annotated[float, Field(ge=0, le=60, description="Seconds before going back to the resting (happy) face; 0 = stay")] = 5,
):
    """Show the player card on the robot's screen: a small face with this expression, the player's name
    and team colour (from the control panel), and its lights in the team colour. Use it in games:
    excited after a goal, sad after a miss, wink after a good pass, surprised after a bump. It goes
    back to the resting face after a few seconds."""
    await robot.ensure_connected()
    try:
        await screen.react(robot, state, expression, caption, hold_s)
    except ValueError as e:
        raise ToolError(str(e)) from None
    return f"Showing {'an' if expression[0] in 'aeiou' else 'a'} {expression} face" + (f" for {state.player}" if state.player else "") + "."


@tool("Play sound")
async def play_sound(
    sound: Literal[SOUNDS] = "tada",  # type: ignore[valid-type]
    volume: Annotated[int, Field(ge=0, le=100)] = 50,
):
    """Play one of the robot's built-in sounds."""
    await robot.ensure_connected()
    await robot.play_sound(sound, volume)
    return f"Playing '{sound}'."


@tool("Play notes")
async def play_notes(
    notes: Annotated[str, Field(description='Notes with optional lengths in ms, e.g. "C5:400 E5:400 G5:800" '
                                            'or "C6 R:200 C6" (R is a rest). Octaves 5-8; # sharp, b flat.')],
    volume: Annotated[int, Field(ge=0, le=100)] = 50,
):
    """Play a tune on the robot's speaker and wait for it to finish (30 seconds at most)."""
    tune = []
    for token in notes.split():
        name, _, ms = token.partition(":")
        duration = int(ms) if ms.isdigit() else 400
        if not 0 < duration <= 4000:
            raise ToolError(f"'{token}': each note can last 1-4000 ms.")
        if name.upper() != "R":
            parse_note(name)  # validate before playing anything
        tune.append((name, duration))
    if not tune or sum(d for _, d in tune) > 30_000:
        raise ToolError("Give between one note and 30 seconds of music.")
    await robot.ensure_connected()
    for name, duration in tune:
        if name.upper() != "R":
            await robot.play_note(name, duration, volume)
        await asyncio.sleep(duration / 1000)
    return f"Played {len(tune)} notes."


@tool("Say")
async def say(
    text: Annotated[str, Field(description="What the robot should say (about 15 seconds of speech at most)")],
    volume: Annotated[int, Field(ge=0, le=100)] = 80,
    voice: Annotated[str | None, Field(description="A macOS voice name, e.g. Samantha or Daniel")] = None,
):
    """Speak out loud through the robot's speaker, using this Mac's text-to-speech."""
    if not shutil.which("say"):
        raise ToolError("Speech uses the macOS 'say' command, which isn't available on this computer.")
    await robot.ensure_connected()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "speech.wav"
        for rate in (16000, 11025, 8000):  # lower sample rates fit more speech into 255 KB
            cmd = ["say", "-o", str(wav), "--file-format=WAVE", f"--data-format=LEI16@{rate}", "-f", "-"]
            if voice:
                cmd[1:1] = ["-v", voice]
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.DEVNULL,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate(text.encode())
            if proc.returncode != 0:
                raise ToolError(f"macOS couldn't turn that into speech: {err.decode().strip()}")
            data = wav.read_bytes()
            if len(data) <= AUDIO_MAX_BYTES:
                await robot.play_audio(data, "wav", volume, "speech.wav")
                return f"Saying it ({len(data) / (rate * 2):.1f} s of speech)."
    raise ToolError("That's too long to say in one go (about 15 seconds at most). Split it up.")


# --------------------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------------------

@tool("Connect to the robot")
async def connect_robot(
    host: Annotated[str | None, Field(description="IP address or hostname, if not the configured robot")] = None,
):
    """Connect, or reconnect, to the robot. Other tools connect automatically, so this is only
    needed to switch robots or to recover after the connection dropped."""
    await robot.connect(host)
    return f"Connected to {robot.host}. Battery {robot.status['robot'].get('battery')}%. {where_now()}"


@tool("Disconnect from the robot")
async def disconnect_robot():
    """Release the robot so VEXcode or a Python script can use it. Tools reconnect automatically."""
    await panel.stop()
    await robot.disconnect()
    return "Disconnected."


def main() -> None:
    """The vex-aim-mcp command. By default it speaks MCP over stdio, for apps that start it themselves.
    --http serves it over streamable HTTP instead, for apps that connect to a URL (ChatGPT, through a
    tunnel), at http://127.0.0.1:<port>/<secret>/mcp: only this computer can reach it directly, and the
    secret path keeps out anyone who doesn't have the address."""
    from . import __version__
    parser = argparse.ArgumentParser(prog="vex-aim-mcp", description="MCP server for a VEX AIM robot.")
    parser.add_argument("--version", action="version", version=f"vex-aim-mcp {__version__}")
    parser.add_argument("--http", action="store_true", help="serve over HTTP, for apps that connect to a URL (e.g. ChatGPT)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AIM_HTTP_PORT") or 8000), help="--http: the port (default 8000)")
    parser.add_argument("--secret", default=os.environ.get("AIM_HTTP_SECRET"),
                        help="--http: the secret part of the address (default: a new random one each start)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    log.info("VEX AIM MCP server for robot at %s", HOST)
    if not args.http:
        mcp.run("stdio")
        return
    secret = args.secret or secrets.token_urlsafe(18)
    path = f"/{secret}/mcp"
    print(f"\nVEX AIM MCP server at http://127.0.0.1:{args.port}{path}\n"
          "Keep the address private: anyone who has it can drive the robot (with the usual motion lock and caps).\n"
          f"For ChatGPT, make it reachable with a tunnel, e.g.  cloudflared tunnel --url http://127.0.0.1:{args.port}\n"
          f"and give ChatGPT  https://<the tunnel's address>{path}\n", file=sys.stderr, flush=True)
    # The secret path is the protection, so don't also insist the Host header says localhost: through a
    # tunnel, it says the tunnel's name.
    mcp.run("streamable-http", host="127.0.0.1", port=args.port, streamable_http_path=path,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


if __name__ == "__main__":
    main()
