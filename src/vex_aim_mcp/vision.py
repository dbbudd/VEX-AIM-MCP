"""Camera-frame helpers: a bearing ruler and detection boxes drawn onto frames (Pillow),
and optional YOLO object detection (Ultralytics), loaded only when first used."""

from __future__ import annotations

import asyncio
import json
import struct
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .paths import data_dir
from .aim_client import VISION_HEIGHT, VISION_WIDTH, AimError, Detection, bearing_deg, vision_x_for_bearing


def fmt_bearing(b: float) -> str:
    """'+12°', '-5°' or '0°' (never '-0°')."""
    r = round(b)
    return f"{r:+d}°" if r else "0°"

RULER_BEARINGS = (-30, -20, -10, 0, 10, 20, 30)
ONBOARD_COLOR = (0, 230, 118)
YOLO_COLOR = (255, 152, 0)


@dataclass
class Box:
    """A detection in camera-frame pixels."""

    label: str
    source: str  # "onboard" (the robot's own AI) or "yolo" (run on this computer)
    x0: float
    y0: float
    x1: float
    y1: float
    bearing: float
    score: float | None = None

    def describe(self) -> str:
        who = "robot's onboard AI" if self.source == "onboard" else "YOLO"
        score = ""
        if self.score is not None:
            score = f", {self.score:.0f}%" if self.score > 1 else f", {self.score:.0%}"
        return (f"{self.label} ({who}{score}): bearing {fmt_bearing(self.bearing)}, "
                f"{self.x1 - self.x0:.0f}×{self.y1 - self.y0:.0f} px box")

    def as_dict(self) -> dict:
        d = {"name": self.label, "bearing_deg": round(self.bearing, 1),
             "box_xyxy": [round(v) for v in (self.x0, self.y0, self.x1, self.y1)]}
        if self.score is not None:
            d["confidence"] = round(self.score / 100 if self.score > 1 else self.score, 2)
        return d


def frame_size(jpeg: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(jpeg)).size  # reads the header only


def onboard_boxes(detections: list[Detection], size: tuple[int, int], name_for=None) -> list[Box]:
    """Scale the robot's 320x240 vision coordinates up to the camera frame. name_for(detection)
    can supply a display name, e.g. one that includes a label the person gave the object."""
    sx, sy = size[0] / VISION_WIDTH, size[1] / VISION_HEIGHT
    return [Box(name_for(d) if name_for else d.name, "onboard", d.x * sx, d.y * sy, (d.x + d.width) * sx,
                (d.y + d.height) * sy, d.bearing, d.score) for d in detections]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def annotate(jpeg: bytes, boxes: list[Box], ruler: bool = True, caption: str | None = None) -> bytes:
    """Draw a bearing ruler along the top edge (degrees left/right of straight ahead) and a
    labelled box for each detection, returning a new JPEG."""
    img = Image.open(BytesIO(jpeg)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    fs = max(12, h // 32)
    font = _font(fs)
    outline = {"stroke_width": 2, "stroke_fill": (0, 0, 0)}
    if ruler:
        for b in RULER_BEARINGS:
            x = vision_x_for_bearing(b) * w / VISION_WIDTH
            tick = 14 if b == 0 else 8
            draw.line([(x, 0), (x, tick)], fill=(255, 255, 255), width=2)
            draw.text((x, tick + 2), fmt_bearing(b), font=font, fill=(255, 255, 255), anchor="ma", **outline)
    for box in boxes:
        color = ONBOARD_COLOR if box.source == "onboard" else YOLO_COLOR
        draw.rectangle([box.x0, box.y0, box.x1, box.y1], outline=color, width=3)
        label = f"{box.label} {fmt_bearing(box.bearing)}"
        draw.text((box.x0 + 3, max(fs * 2 + 4, box.y0 - fs - 4)), label, font=font, fill=color, **outline)
    if caption:
        draw.text((w - 6, h - 6), caption, font=font, fill=(255, 255, 255), anchor="rd", **outline)
    out = BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


class YoloDetector:
    """Ultralytics YOLO on this computer. Recognises the 80 everyday COCO classes (person,
    cup, bottle, chair, sports ball, ...), unlike the robot's onboard AI, which only knows
    VEX game objects. Runs in a separate process (yolo_worker.py) that starts on first use
    and takes about ten seconds to load PyTorch; after that each frame takes ~25 ms."""

    def __init__(self, model_path: str, confidence: float = 0.35) -> None:
        self.model_path = model_path
        self.confidence = confidence
        self._proc: asyncio.subprocess.Process | None = None
        self._starting: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._starting is None

    def warm_up(self) -> None:
        """Start loading the model in the background if it isn't already."""
        if not self.ready and self._starting is None:
            self._starting = asyncio.get_running_loop().create_task(self._start())

    async def _start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(  # in the data folder, where named weights download to
                sys.executable, "-m", "vex_aim_mcp.yolo_worker", self.model_path, str(self.confidence),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, cwd=data_dir())  # stderr: inherited
            reply = await asyncio.wait_for(self._read(), timeout=180)
            if not reply.get("ready"):
                raise AimError(f"YOLO failed to start: {reply}")
        except BaseException:
            await self.close()
            raise
        finally:
            self._starting = None

    async def _read(self) -> dict:
        assert self._proc and self._proc.stdout
        header = await self._proc.stdout.readexactly(4)
        return json.loads(await self._proc.stdout.readexactly(struct.unpack(">I", header)[0]))

    async def detect(self, jpeg: bytes) -> list[Box]:
        async with self._lock:
            if not self.ready:
                self.warm_up()
                try:
                    await asyncio.shield(self._starting) if self._starting else None
                except (OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                    raise AimError(f"YOLO couldn't start ({type(e).__name__}: {e}). Is Ultralytics installed "
                                   f"and the model file at {self.model_path}?") from e
            assert self._proc and self._proc.stdin
            try:
                self._proc.stdin.write(struct.pack(">I", len(jpeg)) + jpeg)
                await self._proc.stdin.drain()
                reply = await asyncio.wait_for(self._read(), timeout=30)
            except (OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                await self.close()
                raise AimError(f"YOLO stopped responding ({type(e).__name__}); it will restart next time.") from e
        if "error" in reply:
            raise AimError(f"YOLO couldn't read that frame: {reply['error']}")
        w, h = reply["size"]
        sx, sy = VISION_WIDTH / w, VISION_HEIGHT / h
        return [Box(name, "yolo", x0, y0, x1, y1, bearing_deg((x0 + x1) / 2 * sx, (y0 + y1) / 2 * sy), conf)
                for name, x0, y0, x1, y1, conf in reply["boxes"]]

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
