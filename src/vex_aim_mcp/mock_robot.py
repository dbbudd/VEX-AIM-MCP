"""A pretend VEX AIM robot for trying the MCP server without hardware.

    ~/.venvs/vex-aim/bin/python mock_robot.py          # listens on 127.0.0.1:8899
    then start the server with AIM_HOST=127.0.0.1:8899

It serves the same four WebSockets as the real robot. Moves and turns are simulated
(heading, position and the busy flags change over time), and the camera sends drawn frames.
By default a blue barrel stands 15° left of the starting direction; --world goals sets up two
goals (two blue and two orange barrels) and a ball. Those worlds only test control flow: things
don't get closer as the robot drives. --world arena is a soccer pitch with real positions:
corner AprilTags, two goals, an obstacle tag and a ball that the robot picks up by driving into it
and can kick (it rolls, slows down and bounces off the walls); the walls stop the robot with a bump.
Everything it receives goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .aim_client import FLAG_CRASHED, FLAG_MOVE_ACTIVE, FLAG_MOVING, FLAG_TURN_ACTIVE, vision_x_for_bearing

log = logging.getLogger("mock_aim")

KNOWN_COMMANDS = {
    "program_init", "drive", "drive_for", "drive_with_vector", "turn", "turn_to", "turn_for", "spin_wheels",
    "set_pose", "lcd_print", "lcd_print_at", "lcd_set_cursor", "lcd_set_origin", "lcd_next_row",
    "lcd_clear_row", "lcd_clear_screen", "lcd_set_font", "lcd_set_pen_width", "lcd_set_pen_color",
    "lcd_set_fill_color", "lcd_draw_line", "lcd_draw_rectangle", "lcd_draw_circle", "lcd_draw_pixel",
    "lcd_draw_image_from_file", "lcd_set_clip_region", "show_emoji", "hide_emoji", "show_aivision",
    "hide_aivision", "imu_calibrate", "imu_set_crash_threshold", "kick_soft", "kick_medium", "kick_hard",
    "play_sound", "play_file", "play_note", "stop_sound", "light_set", "color_description",
    "code_description", "tag_detection", "color_detection", "model_detection",
}
BUSY = FLAG_MOVE_ACTIVE | FLAG_TURN_ACTIVE | FLAG_MOVING

# What the camera can see: (class name, class id, bearing from the starting direction, width,
# height, top edge), sizes in the onboard AI's 320x240 coordinates. Objects don't get closer as
# the robot drives; this is for testing control flow, not physics. A class id of None means the
# onboard AI doesn't know it: it's only detected as a colour, once a colour has been taught. For an
# "AprilTag" the class id is the tag's id; tags are only reported while tag detection is on.
WORLDS = {
    "barrel": [("BlueBarrel", 1, -15, 36, 54, 92)],
    "goals": [("BlueBarrel", 1, -18, 30, 45, 95), ("BlueBarrel", 1, 6, 34, 50, 92),
              ("OrangeBarrel", 2, 150, 30, 45, 95), ("OrangeBarrel", 2, 175, 34, 50, 92),
              ("SportsBall", 0, 70, 24, 22, 118), ("Cup", None, 22, 26, 30, 108),
              ("AprilTag", 3, -27, 24, 24, 62), ("AprilTag", 5, 110, 22, 22, 66)],
}
# The arena: a 1.2 x 2.4 m pitch in field mm (x right, y up the field); the robot starts at (0, -700)
# facing up. Objects: name, class or tag id, x, y, size constant K (width px x distance mm in the
# 320-px view, as in panel.py) and height/width. The blue goal is at the far end, orange behind.
ARENA = {
    "start": (0.0, -700.0),
    "walls": (600.0, 1200.0),  # half-width, half-length
    "objects": [("AprilTag", 0, -560.0, 1160.0, 12000, 1.0), ("AprilTag", 1, 560.0, 1160.0, 12000, 1.0),
                ("AprilTag", 2, -560.0, -1160.0, 12000, 1.0), ("AprilTag", 3, 560.0, -1160.0, 12000, 1.0),
                ("AprilTag", 5, -250.0, 250.0, 12000, 1.0),
                ("BlueBarrel", 1, -150.0, 1100.0, 9000, 1.5), ("BlueBarrel", 1, 150.0, 1100.0, 9000, 1.5),
                ("OrangeBarrel", 2, -150.0, -1100.0, 9000, 1.5), ("OrangeBarrel", 2, 150.0, -1100.0, 9000, 1.5)],
    "ball": (200.0, -100.0),
}
WORLDS["arena"] = []  # its objects live in ARENA
KICK_SPEED = {"soft": 600.0, "medium": 1100.0, "hard": 1700.0}  # mm/s as the ball leaves the kicker
BALL_DECEL = 450.0  # mm/s² of rolling friction
DRIVE_ACCEL = 600.0  # mm/s² while speeding up
COLOURS = {"BlueBarrel": (28, 78, 210), "OrangeBarrel": (240, 120, 20), "SportsBall": (230, 220, 60),
           "Cup": (200, 40, 40), "AprilTag": (245, 245, 245)}


def wrap180(angle: float) -> float:
    return (angle + 180) % 360 - 180


class MockAim:
    START_HEADING = 90.0  # a real robot's raw heading is rarely 0 at connection

    def __init__(self, world: str = "barrel") -> None:
        self.objects = WORLDS[world]
        self.arena = world == "arena"
        self.ball = list(ARENA["ball"])
        self.ball_held, self.ball_speed, self.ball_dir = False, 0.0, 0.0
        self.controller = {"flags": "0x0000", "stick_x": 0, "stick_y": 0, "battery": 0}  # no AIM controller paired
        self.wifi = {"joins": True, "network": "Classroom", "password": "not-a-real-one"}  # what its setup page shows
        self.heading = self.START_HEADING
        self.x = self.y = 0.0
        self.flags = 0
        self.motion: asyncio.Task | None = None
        self.frame_no = 0
        self.font = ImageFont.load_default(size=16)
        self.taught_colours: dict[int, tuple] = {}
        self.colour_detection = False
        self.tag_detection = False

    def in_view(self):
        """(name, class id, left x, top, width, height) of everything in the camera's field of view."""
        if self.arena:
            yield from self._arena_view()
            return
        for name, class_id, bearing, w, h, top in self.objects:
            rel = wrap180(bearing - (self.heading - self.START_HEADING))
            if abs(rel) <= 32:
                yield name, class_id, int(vision_x_for_bearing(rel) - w / 2), top, w, h

    def field_pose(self) -> tuple[float, float, float]:
        """Where the robot is on the arena's pitch: x, y (mm) and heading (0 = up the field)."""
        sx, sy = ARENA["start"]
        return sx - self.y, sy + self.x, (self.heading - self.START_HEADING) % 360

    def _arena_view(self):
        rx, ry, rh = self.field_pose()
        things = list(ARENA["objects"])
        if not self.ball_held:
            things.append(("SportsBall", 0, self.ball[0], self.ball[1], 8700, 1.0))
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
        for _, item in sorted(seen, key=lambda s: -s[0]):  # far first, so near things are drawn on top
            yield item
        if self.ball_held:
            yield "SportsBall", 0, 88, 191, 145, 49  # low and centred, like a real ball in the kicker

    async def ball_loop(self) -> None:
        """The arena's ball: carried in the kicker, rolling after a kick, or waiting to be driven into."""
        while True:
            await asyncio.sleep(0.05)
            rx, ry, rh = self.field_pose()
            if self.ball_held:
                a = math.radians(rh)
                self.ball = [rx + 70 * math.sin(a), ry + 70 * math.cos(a)]
            elif self.ball_speed > 0:
                a = math.radians(self.ball_dir)
                self.ball[0] += self.ball_speed * 0.05 * math.sin(a)
                self.ball[1] += self.ball_speed * 0.05 * math.cos(a)
                self.ball_speed = max(0.0, self.ball_speed - BALL_DECEL * 0.05)
                wx, wy = ARENA["walls"]
                if abs(self.ball[0]) > wx - 20:  # bounce off a side wall, losing half its speed
                    self.ball[0] = math.copysign(wx - 20, self.ball[0])
                    self.ball_dir, self.ball_speed = (-self.ball_dir) % 360, self.ball_speed * 0.5
                if abs(self.ball[1]) > wy - 20:
                    self.ball[1] = math.copysign(wy - 20, self.ball[1])
                    self.ball_dir, self.ball_speed = (180 - self.ball_dir) % 360, self.ball_speed * 0.5
            else:
                d = math.hypot(self.ball[0] - rx, self.ball[1] - ry)
                rel = wrap180(math.degrees(math.atan2(self.ball[0] - rx, self.ball[1] - ry)) - rh)
                if d < 100 and abs(rel) < 15:  # driven into: the kicker's magnet holds it
                    self.ball_held = True

    def visible(self) -> list[dict]:
        items = []
        for name, class_id, x, top, w, h in self.in_view():
            if name == "AprilTag":
                if self.tag_detection:
                    items.append({"type": 8, "id": class_id, "type_str": "tag", "originx": x, "originy": top,
                                  "width": w, "height": h, "name": ""})
            elif class_id is not None:
                items.append({"type": 4, "id": class_id, "type_str": "aiobj", "originx": x,
                              "originy": top, "width": w, "height": h, "score": 91, "name": name})
            elif self.colour_detection:  # any taught colour stands in for the unknown object's colour
                items += [{"type": 1, "id": cid, "type_str": "color", "originx": x, "originy": top,
                           "width": w, "height": h, "angle": 0, "name": ""} for cid in self.taught_colours]
        return items

    def status(self) -> dict:
        objects = self.visible()
        return {
            "controller": dict(self.controller),
            "robot": {
                "flags": f"0x{self.flags:08x}", "battery": 87, "touch_flags": "0x0000", "touch_x": 0, "touch_y": 0,
                "robot_x": round(self.x, 1), "robot_y": round(self.y, 1), "roll": "0.02", "pitch": "0.11",
                "yaw": f"{wrap180(self.heading):.2f}", "heading": f"{self.heading % 360:.2f}",
                "rotation": f"{self.heading:.2f}", "acceleration": {"x": "0", "y": "0", "z": "-1"},
                "gyro_rate": {"x": "0", "y": "0", "z": "0"}, "screen": {"row": "1", "column": "1"},
            },
            "aivision": {
                "classnames": {"count": 4, "items": [{"index": 0, "name": "SportsBall"}, {"index": 1, "name": "BlueBarrel"},
                                                     {"index": 2, "name": "OrangeBarrel"}, {"index": 3, "name": "Robot"}]},
                "objects": {"count": len(objects), "items": objects or [{"type": 0, "id": 0, "originx": 0, "originy": 0,
                                                                         "width": 0, "height": 0, "score": 0, "name": "0"}]},
            },
        }

    def handle(self, cmd: dict) -> dict:
        cid = cmd.get("cmd_id", "")
        log.info("command %s", json.dumps(cmd))
        if cid == "mock_controller":  # for tests only: pretend an AIM controller is paired and being used
            self.controller.update({k: cmd[k] for k in ("flags", "stick_x", "stick_y", "battery") if k in cmd})
            return {"cmd_id": cid, "status": "complete"}
        if cid == "mock_ball_in_kicker" and self.arena:  # for tests only: the real robot has no such command
            self.ball_held, self.ball_speed = True, 0.0
            return {"cmd_id": cid, "status": "complete"}
        if cid not in KNOWN_COMMANDS:
            return {"cmd_id": "cmd_unknown"}
        if cid == "drive_for":
            self._start(self._drive(cmd["distance"], cmd["angle"], cmd["drive_speed"]))
            return {"cmd_id": cid, "status": "in_progress"}
        if cid == "turn_for":
            self._start(self._turn(cmd["angle"], cmd["turn_rate"]))
            return {"cmd_id": cid, "status": "in_progress"}
        if cid == "turn_to":
            self._start(self._turn(wrap180(cmd["heading"] - self.heading), cmd["turn_rate"]))
            return {"cmd_id": cid, "status": "in_progress"}
        if (cid == "drive" and cmd.get("speed") == 0) or (cid == "turn" and cmd.get("turn_rate") == 0):
            self._halt()
        if cid == "spin_wheels":  # driver control: the three omni-wheels' speeds, mixed as vex/aim.py does
            w1, w2, w3 = (float(cmd.get(k, 0)) for k in ("vel1", "vel2", "vel3"))
            spin = (w1 + w2 + w3) / 3  # °/s clockwise
            sideways, forwards = spin - w3, (w1 - w2) / 1.732  # mm/s
            if abs(sideways) < 1 and abs(forwards) < 1 and abs(spin) < 1:
                self._halt()
            else:
                self._start(self._free_drive(sideways, forwards, spin))
        if cid == "color_description":
            self.taught_colours[cmd["id"]] = (cmd["red"], cmd["green"], cmd["blue"])
        if cid == "color_detection":
            self.colour_detection = bool(cmd.get("b_enable"))
        if cid == "tag_detection":
            self.tag_detection = bool(cmd.get("b_enable"))
        if cid.startswith("kick_") and self.arena and self.ball_held:
            self.ball_held, self.ball_speed, self.ball_dir = False, KICK_SPEED[cid[5:]], self.field_pose()[2]
        return {"cmd_id": cid, "status": "complete"}

    def _start(self, coro) -> None:
        self._halt()
        self.motion = asyncio.get_running_loop().create_task(coro)

    def _halt(self) -> None:
        if self.motion and not self.motion.done():
            self.motion.cancel()
        self.flags &= ~BUSY

    async def _drive(self, distance: float, angle: float, speed: float) -> None:
        """Speed up at DRIVE_ACCEL to the requested speed (mm/s), and stop at an arena wall with a bump."""
        self.flags |= FLAG_MOVE_ACTIVE | FLAG_MOVING
        try:
            theta = math.radians(self.heading + angle)
            sign, left, v = math.copysign(1, distance), abs(distance), 0.0
            while left > 0:
                await asyncio.sleep(0.05)
                v = min(max(speed, 1), v + DRIVE_ACCEL * 0.05)
                step = min(left, v * 0.05)
                left -= step
                self.x += sign * step * math.sin(theta)
                self.y += sign * step * math.cos(theta)
                if self.arena:
                    fx, fy, _ = self.field_pose()
                    wx, wy = ARENA["walls"]
                    if abs(fx) > wx - 80 or abs(fy) > wy - 80:  # the robot's edge reached a wall
                        self.x -= sign * step * math.sin(theta)
                        self.y -= sign * step * math.cos(theta)
                        self.flags |= FLAG_CRASHED
                        asyncio.get_running_loop().call_later(0.6, lambda: setattr(self, "flags", self.flags & ~FLAG_CRASHED))
                        break
        finally:
            self.flags &= ~BUSY

    async def _free_drive(self, sideways: float, forwards: float, spin: float) -> None:
        """Keep moving at these speeds (mm/s right and forward, °/s clockwise) until told otherwise."""
        self.flags |= FLAG_MOVING
        try:
            while True:
                await asyncio.sleep(0.05)
                theta = math.radians(self.heading)
                dx = (forwards * math.sin(theta) + sideways * math.sin(theta + math.pi / 2)) * 0.05
                dy = (forwards * math.cos(theta) + sideways * math.cos(theta + math.pi / 2)) * 0.05
                self.x, self.y = self.x + dx, self.y + dy
                self.heading = (self.heading + spin * 0.05) % 360
                if self.arena:
                    fx, fy, _ = self.field_pose()
                    wx, wy = ARENA["walls"]
                    if abs(fx) > wx - 80 or abs(fy) > wy - 80:  # into a wall: back off a step and bump
                        self.x, self.y = self.x - dx, self.y - dy
                        self.flags |= FLAG_CRASHED
                        asyncio.get_running_loop().call_later(0.6, lambda: setattr(self, "flags", self.flags & ~FLAG_CRASHED))
        finally:
            self.flags &= ~BUSY

    async def _turn(self, degrees: float, rate: float) -> None:
        self.flags |= FLAG_TURN_ACTIVE | FLAG_MOVING
        try:
            duration = abs(degrees) / max(abs(rate), 1)
            steps = max(1, int(duration / 0.05))
            for _ in range(steps):
                await asyncio.sleep(duration / steps)
                self.heading = (self.heading + degrees / steps) % 360
        finally:
            self.flags &= ~BUSY

    def frame(self) -> bytes:
        self.frame_no += 1
        img = Image.new("RGB", (640, 480), (72, 74, 82))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 250, 640, 480], fill=(150, 118, 86))  # floor
        for name, class_id, x, top, w, h in self.in_view():
            if w <= 0:
                continue
            box = [x * 2, top * 2, (x + w) * 2, (top + h) * 2]
            (draw.ellipse if name == "SportsBall" else draw.rectangle)(box, fill=COLOURS[name])
            if name == "AprilTag":  # a card with a black square and its id, roughly like the real thing
                inset = w // 4
                draw.rectangle([box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset], fill=(20, 20, 20))
                draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), str(class_id), font=self.font,
                          fill=(255, 255, 255), anchor="mm")
        draw.text((320, 466), f"simulated camera · frame {self.frame_no}", font=self.font, fill=(255, 255, 255), anchor="mm")
        out = BytesIO()
        img.save(out, format="JPEG", quality=80)
        return out.getvalue()


SETUP_PAGE = """<!DOCTYPE html><html lang='en' class=''><head><title>AIM - Configure WiFi</title></head><body>
<h3>AIM Setup</h3><p style='font-family: monospace;'> AIM VEXOS&nbsp;version&nbsp;:&nbsp;1.0.3.0 <br/>
AIM Radio&nbsp;version&nbsp;:&nbsp;1.0.3.0 <br/><p><form id='wifi' method='get' action='wifi'>
<input id='f1' type='checkbox' onclick='di()' {checked}/> <input id='flags' type='hidden' value='0'>
<input id='s1' placeholder='Enter your WiFi Network' maxlength='20' value='{network}'>
<input id='p1' type='password' placeholder='Enter your WiFi Password' maxlength='20' value='{password}'>
<input id='a1' placeholder='IP address for AP' value='0.0.0.0'></form></body></html>"""


def setup_page(mock: MockAim):
    """The robot's own web page (/ and /wifi), like the real one: it shows the saved network and, like the real
    one, its password. Saving records the new settings (the simulator doesn't actually change networks)."""
    from urllib.parse import parse_qs, urlsplit

    def respond(connection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        url = urlsplit(request.path)
        if url.path == "/wifi":
            q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            mock.wifi = {"joins": q.get("flags") == "1", "network": q.get("s1", mock.wifi["network"]),
                         "password": q.get("p1", mock.wifi["password"]), "a1": q.get("a1", "0.0.0.0")}
            log.info("setup page: saved, join %r", mock.wifi["network"])
            return connection.respond(200, "Saved. Restarting the radio.")
        if url.path == "/":
            w = mock.wifi
            return connection.respond(200, SETUP_PAGE.format(checked="checked" if w["joins"] else "", network=w["network"], password=w["password"]))
        return connection.respond(404, "Not found")

    return respond


async def handle_client(ws: ServerConnection, mock: MockAim) -> None:
    path = ws.request.path
    log.info("client connected to %s", path)
    try:
        if path == "/ws_status":
            async for _ in ws:
                await ws.send(json.dumps(mock.status()))
        elif path == "/ws_cmd":
            async for msg in ws:
                try:
                    reply = mock.handle(json.loads(msg))
                except (ValueError, KeyError):
                    reply = {"cmd_id": "cmd_unknown"}
                await ws.send(json.dumps(reply))
        elif path == "/ws_img":
            streaming = asyncio.Event()

            async def send_frames():
                while True:
                    await streaming.wait()
                    await ws.send(mock.frame())
                    await asyncio.sleep(0.1)  # ~10 fps

            sender = asyncio.create_task(send_frames())
            try:
                async for msg in ws:
                    (streaming.set if msg == b"\x01" else streaming.clear)()
            finally:
                sender.cancel()
        elif path == "/ws_audio":
            async for msg in ws:
                kind = "wav" if msg[0] == 0 else "mp3"
                name = msg[32:64].rstrip(b"\0").decode(errors="replace")
                log.info("audio: %s, volume %d, %d bytes, name %r", kind, msg[1], int.from_bytes(msg[4:8], "little"), name)
        else:
            await ws.close(1008, "unknown endpoint")
    except ConnectionClosed:
        pass
    log.info("client left %s", path)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Pretend to be a VEX AIM robot.")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--world", choices=sorted(WORLDS), default="barrel",
                        help="barrel: one blue barrel; goals: two blue and two orange barrels and a ball; "
                             "arena: a soccer pitch with tags, goals and a kickable ball")
    parser.add_argument("--ball-in-kicker", action="store_true", help="arena: start with the ball in the kicker")
    args = parser.parse_args()
    mock = MockAim(args.world)
    mock.ball_held = mock.arena and args.ball_in_kicker
    if mock.arena:
        asyncio.get_running_loop().create_task(mock.ball_loop())
    async with serve(lambda ws: handle_client(ws, mock), "127.0.0.1", args.port, compression=None,
                     process_request=setup_page(mock)):
        log.info("mock AIM robot listening on 127.0.0.1:%d", args.port)
        await asyncio.Future()



def main() -> None:
    """The vex-aim-sim command."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    asyncio.run(_main())


if __name__ == "__main__":
    main()
