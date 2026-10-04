"""What the person, Claude and the robot's routines share: the team, labels, taught colours,
distance measurements, the map, AprilTag roles, the playing field, measured abilities, and a log of
who did what. Used by the control panel (panel.py), Claude's tools (server.py) and routines.py."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import time
from collections import deque
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageStat

from .aim_client import AimError, AimRobot, Detection
from .paths import data_dir

log = logging.getLogger("vex_aim.panel")

# Rough distance from apparent width in the onboard AI's 320-px view: mm ≈ K / width, where K is the
# camera's focal length (about 234 px for AIM's ±34° view) times the object's real width. The ball's
# constant was calibrated on a real robot; the others are estimates. Taught colours get one when the
# person gives the object's real width. Objects of unknown size have no distance, only a direction.
FOCAL_PX = 234
# what's worth keeping between runs: distance measurements, AprilTag roles and the field set-up
SETUP_FILE = Path(os.environ.get("AIM_PANEL_SETUP") or data_dir() / "panel_setup.json")
SIZE_K = {"SportsBall": 8700, "BlueBarrel": 9000, "OrangeBarrel": 9000, "Robot": FOCAL_PX * 140}
FOV_DEG = 34
MAP_MERGE_MM = 100  # sightings this close (or within 25% of the distance, if more) update a remembered object
UNIQUE_OBJECTS = {"SportsBall"}  # one ball per game: a new sighting moves it rather than adding another
MOVERS = {"SportsBall", "Robot"}  # things that move by themselves: the map tracks their velocity
MAP_CONFIRM = 3  # sightings needed before an object appears on the map (stops one-off glitches)

# Playing fields the map can show, in mm: width (left to right) and length (up the field). Field
# coordinates put (0, 0) at the centre, x to the right and y up the field: the way the robot faced
# (heading 0) when it connected, so start it facing up the field.
FIELDS = {
    "desk": {"name": "desk 1 × 1 m", "w": 1000, "l": 1000, "tile": 100, "tile_label": "10 cm"},
    "soccer": {"name": "soccer pitch 1.2 × 2.4 m", "w": 1200, "l": 2400, "tile": 200, "tile_label": "20 cm"},
    "iq": {"name": "VEX IQ field 6 × 8 ft", "w": 1829, "l": 2438, "tile": 304.8, "tile_label": "1 ft"},
    "v5": {"name": "VEX V5 field 12 × 12 ft", "w": 3658, "l": 3658, "tile": 609.6, "tile_label": "2 ft"},
}
TAG_ROLES = ("marker", "obstacle")  # a marker is a landmark (pin it to the field); an obstacle is to be avoided
OBSTACLE_MM = 150  # keep-out radius around an obstacle tag
LOCATE_MAX_MM = 1500  # markers further away than this are too uncertain to locate the robot by


def calibration_key(d: Detection) -> str:
    """Objects that share a size share a distance constant: one per AI class, one for all AprilTags
    (printed the same size), one per taught colour."""
    if d.kind == "apriltag":
        return "AprilTag"
    if d.kind == "color":
        return f"colour {d.id}"
    return d.name


def cut_off(d: Detection) -> bool:
    """Touching the edge of the picture, so only partly visible: its width (and distance) is wrong."""
    return d.x <= 1 or d.x + d.width >= 319 or d.y <= 1 or d.y + d.height >= 239

# Hue range (degrees) and saturation range (0-1) for a taught colour, as in VEX's AI Vision Utility.
COLOUR_TOLERANCE = {"tight": (6.0, 0.15), "normal": (10.0, 0.20), "loose": (18.0, 0.30)}
MAX_COLOURS = 7


def wrap180(angle: float) -> float:
    return (angle + 180) % 360 - 180


class PanelState:
    """What the person and Claude share: the team, object labels, taught colours, the last thing
    the person pointed out, and a log of who did what."""

    def __init__(self, setup_file: Path | None = SETUP_FILE) -> None:
        self.setup_file = setup_file
        self.team: str | None = None
        self.labels: list[dict] = []  # {"name", "label", "heading"} or {"name", "label", "tag"}
        self.colours: dict[int, dict] = {}  # robot colour id -> {"label", "rgb", "tolerance"}
        self.selected: dict | None = None
        self.map: list[dict] = []  # remembered objects, in the odometry frame (mm), built while the panel runs
        self.calibration: dict[str, list[list[float]]] = {}  # key -> [width px, true distance mm] samples
        self.smallest: dict[str, int] = {}  # key -> smallest uncut width the onboard AI has detected
        self.events: deque[dict] = deque(maxlen=300)
        self.event_seq = 0
        self.actions: deque[dict] = deque(maxlen=50)  # the person's actions, for wait_for
        self.tags: dict[int, dict] = {}  # AprilTag id -> {"role": "marker"|"obstacle"|None, "pin": [x, y] on the field}
        # Which field the map shows, and where the robot's odometry frame sits on it (field = odometry + offset).
        # "located_by" says how that offset was found: by hand, or from a pinned marker in view.
        self.field: dict = {"name": "fit", "offset": [0.0, 0.0], "located_by": None}
        # Measured by experiments (routines.py): where kicks stop, mm, per strength; and drive speeds by speed %.
        self.abilities: dict = {"kick": {}, "drive": {}}
        self.player: str | None = None  # this robot's player name, shown on its screen
        self.mode = "auto"  # "auto": routines, plays and Claude may drive; "driver": the person drives from the panel
        self.venues: list[str] = []  # Wi-Fi networks saved for moving robots onto (passwords are in the keychain)
        self.roster: list[dict] = []  # the team list, as fleet.Fleet.to_dict() saves it
        self.match: dict | None = None  # a match in progress: phase ("auto" or "driver"), when it ends, durations
        self.routine: dict | None = None  # the job the robot is doing by itself: name, progress text, task
        self.last_kick: dict | None = None  # the latest kick test, so the panel can ask where the ball stopped
        self.battery_log: deque[tuple[float, int]] = deque(maxlen=2000)  # (time, %), for the charge/run-time trend
        self._battery_saved = 0.0
        self._changed: asyncio.Condition | None = None
        self._load_setup()

    def _load_setup(self) -> None:
        if not self.setup_file or not self.setup_file.exists():
            return
        try:
            data = json.loads(self.setup_file.read_text())
            self.calibration = {str(k): [[float(w), float(mm)] for w, mm in v]
                                for k, v in (data.get("calibration") or {}).items()}
            self.tags = {int(k): {"role": v.get("role") if v.get("role") in TAG_ROLES else None,
                                  "pin": [float(c) for c in v["pin"]] if v.get("pin") else None,
                                  "note": str(v["note"])[:160] if v.get("note") else None}
                         for k, v in (data.get("tags") or {}).items()}
            self.labels += [{"name": f"AprilTag {int(e['tag'])}", "label": str(e["label"])[:30], "tag": int(e["tag"])}
                            for e in data.get("tag_labels") or []]
            if data.get("field") in FIELDS:
                self.field["name"] = data["field"]
            kicks = (data.get("abilities") or {}).get("kick") or {}
            drives = (data.get("abilities") or {}).get("drive") or {}
            launch = (data.get("abilities") or {}).get("launch") or {}
            self.abilities = {"kick": {str(k): [float(v) for v in vs] for k, vs in kicks.items() if k in ("soft", "medium", "hard")},
                              "drive": {str(k): dict(v) for k, v in drives.items()},
                              "launch": {str(k): [float(v) for v in vs] for k, vs in launch.items()},
                              "slowing": [float(v) for v in (data.get("abilities") or {}).get("slowing") or []]}
            self.player = str(data["player"])[:16] if data.get("player") else None
            self.team = data.get("team") if data.get("team") in ("blue", "orange") else None
            self.venues = [str(v)[:20] for v in data.get("venues") or []]
            self.roster = [e for e in data.get("fleet") or [] if isinstance(e, dict)]
            now = time.time()  # recent battery readings survive a restart, so the estimate doesn't start over
            self.battery_log.extend((float(t), int(p)) for t, p in data.get("battery_log") or [] if now - float(t) < 1800)
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.warning("couldn't read %s: %s", self.setup_file, e)

    def save_setup(self) -> None:
        """Write what's worth keeping to disk: measurements, tag roles, pins and labels, and the field."""
        if not self.setup_file:
            return
        data = {"calibration": self.calibration, "tags": self.tags, "field": self.field["name"],
                "tag_labels": [e for e in self.labels if "tag" in e], "abilities": self.abilities, "player": self.player,
                "team": self.team, "venues": self.venues, "fleet": self.roster, "battery_log": [[round(t), p] for t, p in self.battery_log if time.time() - t < 1800]}
        try:
            tmp = self.setup_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.setup_file)
        except OSError as e:
            log.warning("couldn't save %s: %s", self.setup_file, e)

    def on_field(self, x: float, y: float) -> tuple[float, float]:
        """A point in the robot's odometry frame, in field coordinates (unchanged when no field is chosen)."""
        if self.field["name"] not in FIELDS:
            return x, y
        return x + self.field["offset"][0], y + self.field["offset"][1]

    def kick_reach(self) -> dict:
        """Per kick strength with tests: how far the ball goes (median, mm), the spread and how many tests."""
        launch = self.abilities.get("launch") or {}
        return {s: {"mm": round(statistics.median(v)), "min_mm": round(min(v)), "max_mm": round(max(v)), "tests": len(v),
                    "launch_mm_s": round(statistics.median(launch[s])) if launch.get(s) else None}
                for s in ("soft", "medium", "hard") if (v := self.abilities["kick"].get(s))}

    def note_battery(self, percent: int | None) -> None:
        """Keep a reading when the level changes, or once a minute."""
        if percent is None:
            return
        now = time.time()
        if not self.battery_log or self.battery_log[-1][1] != percent or now - self.battery_log[-1][0] > 60:
            self.battery_log.append((now, int(percent)))
            if now - self._battery_saved > 60:  # keep them across restarts, without writing more than once a minute
                self._battery_saved = now
                self.save_setup()

    def battery_trend(self) -> dict:
        """Whether the battery is charging (the robot doesn't say if it's plugged in, so this is read from the
        level rising or falling) and how long until it's full or flat, from the last 20 minutes."""
        if not self.battery_log:
            return {"charging": None}
        now, pct = time.time(), self.battery_log[-1][1]
        recent = [(t, p) for t, p in self.battery_log if now - t < 1200]
        span_min = (recent[-1][0] - recent[0][0]) / 60
        if max(p for _, p in recent) - min(p for _, p in recent) < 2:  # not enough change to tell yet
            return {"charging": None, "watching_min": round(span_min, 1), "steady": span_min >= 10}
        if span_min < 3:
            return {"charging": None, "watching_min": round(span_min, 1)}
        mt, mp = statistics.fmean(t for t, _ in recent), statistics.fmean(p for _, p in recent)
        slope = sum((t - mt) * (p - mp) for t, p in recent) / sum((t - mt) ** 2 for t, _ in recent) * 60  # % per minute
        out = {"charging": slope > 0, "pct_per_min": round(slope, 2)}
        if slope > 0:
            out["minutes_to_full"] = round((100 - pct) / slope)
        elif slope < 0:
            out["minutes_left"] = round(pct / -slope)
        return out

    def routine_status(self) -> dict | None:
        r = self.routine
        if not r or r["task"].done():
            return None
        return {"name": r["name"], "text": r["text"], "seconds": round(time.time() - r["started"])}

    def tag_name(self, tag_id: int) -> str:
        label = next((e["label"] for e in self.labels if e.get("tag") == tag_id), None)
        return f"AprilTag {tag_id}" + (f" “{label}”" if label else "")

    def log(self, who: str, text: str) -> None:
        self.event_seq += 1
        self.events.append({"seq": self.event_seq, "t": time.time(), "who": who, "text": text})
        log.info("[%s] %s", who, text)

    def action(self, text: str, **details) -> None:
        """Something the person did in the panel: logged, and woken up anyone in wait_for."""
        self.log("you", text)
        self.actions.append({"seq": self.event_seq, "text": text, **details})
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self._condition():
            self._condition().notify_all()

    def _condition(self) -> asyncio.Condition:
        if self._changed is None:
            self._changed = asyncio.Condition()
        return self._changed

    async def wait_action(self, after_seq: int, timeout: float) -> dict | None:
        """The first action the person takes after event number after_seq, or None on timeout."""
        cond = self._condition()
        async with cond:
            try:
                await asyncio.wait_for(cond.wait_for(lambda: any(a["seq"] > after_seq for a in self.actions)), timeout)
            except TimeoutError:
                return None
        return next(a for a in self.actions if a["seq"] > after_seq)

    def label_at(self, name: str, heading: float) -> str | None:
        """The label for an object of this kind seen at this heading (within 10°), if any."""
        best = None
        for entry in self.labels:
            if entry["name"] == name and entry.get("heading") is not None:
                off = abs(wrap180(heading - entry["heading"]))
                if off < 10 and (best is None or off < best[0]):
                    best = (off, entry["label"])
        return best[1] if best else None

    def label_for(self, d: Detection, robot_heading: float) -> str | None:
        if d.kind == "color":
            return (self.colours.get(d.id) or {}).get("label")
        if d.kind == "apriltag":
            return next((e["label"] for e in self.labels if e.get("tag") == d.id), None)
        return self.label_at(d.name, (robot_heading + d.bearing) % 360)

    def display_name(self, d: Detection, robot_heading: float) -> str:
        label = self.label_for(d, robot_heading)
        if d.kind == "color":
            return label or d.name
        return f"{d.name} “{label}”" if label else d.name

    def size_constant(self, key: str) -> float | None:
        """K (px x mm) measured with the calibration tool: the median of width x true distance."""
        samples = self.calibration.get(key)
        return statistics.median(w * mm for w, mm in samples) if samples else None

    def camera_range(self) -> dict:
        """Per kind of object: its distance constant, how many measurements back it, the smallest it
        has been detected at, and so roughly how far away the onboard AI can still see it."""
        out = {}
        for key in sorted(set(self.calibration) | set(self.smallest) | {"SportsBall", "BlueBarrel", "OrangeBarrel"}):
            k = self.size_constant(key) or SIZE_K.get(key)
            if key.startswith("colour ") and not k:
                width = (self.colours.get(int(key.split()[1])) or {}).get("width_mm")
                k = FOCAL_PX * width if width else None
            smallest = self.smallest.get(key)
            out[key] = {"k": round(k) if k else None, "measured": len(self.calibration.get(key, [])),
                        "smallest_px": smallest, "range_mm": round(k / smallest) if k and smallest else None}
        return out

    def summary(self, pose: tuple[float, float, float] | None = None) -> dict:
        """For Claude. pose is the robot's (x, y, heading) in its odometry frame, if connected."""
        now = time.monotonic()
        field = FIELDS.get(self.field["name"])
        out = {"team": self.team, "labels": self.labels,
               "taught_colours": {i: c["label"] for i, c in self.colours.items()},
               "last_pointed_out": self.selected,
               "map": [{"name": o["display"], "x_mm": round(self.on_field(o["x"], o["y"])[0]),
                        "y_mm": round(self.on_field(o["x"], o["y"])[1]), "seen_s_ago": round(now - o["seen"])}
                       for o in self.map if o["sightings"] >= MAP_CONFIRM],
               "map_frame": (f"the {field['name']}: (0, 0) is its centre, x to the right, y up the field"
                             if field else "odometry: (0, 0) is where the robot connected, y the way it faced"),
               "camera_range": self.camera_range()}
        if field:
            out["field"] = {"name": field["name"], "size_mm": [field["w"], field["l"]],
                            "located_by": self.field["located_by"]}
            if pose:
                out["field"]["robot_mm"] = [round(v) for v in self.on_field(pose[0], pose[1])]
        if self.tags:
            out["apriltags"] = [{"id": i, "name": self.tag_name(i), "role": t["role"],
                                 **({"meaning": t["note"]} if t.get("note") else {}),
                                 **({"pinned_mm": [round(c) for c in t["pin"]]} if t.get("pin") and field else {})}
                                for i, t in sorted(self.tags.items())]
        obstacles = []
        for i, t in self.tags.items():
            if t["role"] != "obstacle":
                continue
            where = t["pin"] if t.get("pin") and field else next(
                (self.on_field(o["x"], o["y"]) for o in self.map if o["kind"] == "apriltag" and o["id"] == i), None)
            if not where:
                continue
            entry = {"name": self.tag_name(i), "x_mm": round(where[0]), "y_mm": round(where[1]), "keep_out_mm": OBSTACLE_MM}
            if pose:
                rx, ry = self.on_field(pose[0], pose[1])
                entry["distance_mm"] = round(math.dist((rx, ry), where))
                entry["bearing_deg"] = round(wrap180(math.degrees(math.atan2(where[0] - rx, where[1] - ry)) - pose[2]))
            obstacles.append(entry)
        if obstacles:
            out["obstacles"] = obstacles
        if self.abilities["kick"] or self.abilities["drive"]:
            out["abilities"] = {"kick_reach": self.kick_reach(),
                                "drive_speed_mm_s": {f"{k}%": v["top_mm_s"] for k, v in sorted(self.abilities["drive"].items())}}
        if self.player:
            out["player"] = self.player
        if routine := self.routine_status():
            out["busy_with"] = routine
        out["mode"] = self.mode
        if self.match:
            out["match"] = {"phase": self.match["phase"], "seconds_left": max(0, round(self.match["ends"] - time.time()))}
        if (trend := self.battery_trend()).get("charging") is not None:
            out["battery"] = trend
        return out


def approx_distance_mm(d: Detection, state: PanelState | None = None) -> float | None:
    """Distance from apparent width. A constant measured with the calibration tool wins; otherwise the
    built-in estimate for balls, barrels and robots, or a taught colour's real width. AprilTags and
    other colours have no distance until measured."""
    k = state.size_constant(calibration_key(d)) if state else None
    if k is None and d.kind == "ai_object":
        k = SIZE_K.get(d.name)
    if k is None and d.kind == "color" and state and (width := (state.colours.get(d.id) or {}).get("width_mm")):
        k = FOCAL_PX * width
    return round(k / d.width) if k and d.width else None


async def teach_colour(robot: AimRobot, state: PanelState, label: str, box: list[float],
                       tolerance: str = "normal", width_mm: float | None = None) -> dict:
    """Sample the colour inside box (x0, y0, x1, y1 in 640x480 camera pixels) from a fresh frame and
    teach it to the robot's onboard AI as a colour signature, which it then detects on its own.
    width_mm, the object's real width, lets its distance be estimated (and so mapped)."""
    label = label.strip()[:30]
    if not label:
        raise AimError("Give the colour a name, e.g. 'red cup'.")
    jpeg = await robot.camera_frame()
    img = Image.open(BytesIO(jpeg)).convert("RGB")
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, x1 = sorted((max(0, min(img.width, x0)), max(0, min(img.width, x1))))
    y0, y1 = sorted((max(0, min(img.height, y0)), max(0, min(img.height, y1))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise AimError("That box is too small to sample a colour from; drag over more of the object.")
    rgb = tuple(int(v) for v in ImageStat.Stat(img.crop((x0, y0, x1, y1))).median)
    colour_id = next((i for i, c in state.colours.items() if c["label"] == label), None)
    if colour_id is None:
        colour_id = next((i for i in range(1, MAX_COLOURS + 1) if i not in state.colours), None)
    if colour_id is None:
        raise AimError(f"The robot can remember {MAX_COLOURS} colours; forget one first.")
    hue_range, saturation_range = COLOUR_TOLERANCE.get(tolerance, COLOUR_TOLERANCE["normal"])
    await robot.set_colour(colour_id, rgb, hue_range, saturation_range)
    await robot.set_detection(colours=True)
    width = float(width_mm) if width_mm and float(width_mm) > 0 else None
    state.colours[colour_id] = {"label": label, "rgb": list(rgb), "tolerance": tolerance, "width_mm": width}
    return {"id": colour_id, "label": label, "rgb": list(rgb), "width_mm": width}
