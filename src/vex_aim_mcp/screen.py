"""The robot's player card: its 240x240 screen (seen through a round window) shows the player's name
on the team colour, with a small drawn face whose expression changes with the game, and an
optional caption ("GOAL!"). The robot's own emoji fill the whole screen, so the face here is drawn
with circles, rectangles and lines instead."""

from __future__ import annotations

import asyncio
import contextlib
import math

from .aim_client import AimRobot

EXPRESSIONS = ("happy", "excited", "sad", "wink", "surprised", "focused")
RESTING = "happy"  # the face the card goes back to after a reaction
REACTION_S = 5.0
CAPTIONS = {"excited": "GOAL!", "sad": "So close!", "wink": "Nice pass!", "surprised": "Whoa!"}
TEAM_RGB = {"blue": (0, 90, 255), "orange": (255, 110, 0), None: (40, 40, 40)}
# Monospaced fonts: width of one character and the text's height above the baseline, in pixels. VEX's mono
# fonts are half as wide as their size (as on the V5 brain: mono20 is 10 px wide).
FONTS = [("mono40", 20, 28), ("mono30", 15, 21), ("mono20", 10, 14)]
FACE, INK, WHITE = (255, 205, 40), (45, 30, 0), (255, 255, 255)
CX, CY, R = 120, 108, 44  # where the face sits


def _fit(text: str, half_chord: float) -> tuple[str, float, int]:
    """The biggest font that fits text across the round window at that height."""
    for font, char_w, height in FONTS:
        if len(text) * char_w <= 2 * half_chord - 12:
            return font, len(text) * char_w, height
    font, char_w, height = FONTS[-1]
    return font, len(text) * char_w, height


async def _text(robot: AimRobot, text: str, baseline: int, rgb: tuple[int, int, int]) -> None:
    half_chord = math.sqrt(max(0, 120 ** 2 - (baseline - 8 - 120) ** 2))  # the window's width at that height
    font, width, _ = _fit(text, half_chord)
    await robot.command("lcd_set_font", fontname=font)
    await robot.command("lcd_set_pen_color", r=rgb[0], g=rgb[1], b=rgb[2])
    await robot.command("lcd_print_at", x=round(120 - width / 2), y=baseline, string=text, b_opaque=False)


async def _circle(robot: AimRobot, x: int, y: int, radius: int, fill: tuple, outline: tuple | None = None) -> None:
    pen = outline or fill
    await robot.command("lcd_set_pen_color", r=pen[0], g=pen[1], b=pen[2])
    await robot.command("lcd_draw_circle", x=x, y=y, radius=radius, r=fill[0], g=fill[1], b=fill[2], b_transparency=False)


async def _rect(robot: AimRobot, x: int, y: int, w: int, h: int, fill: tuple) -> None:
    await robot.command("lcd_set_pen_color", r=fill[0], g=fill[1], b=fill[2])
    await robot.command("lcd_draw_rectangle", x=x, y=y, width=w, height=h, r=fill[0], g=fill[1], b=fill[2], b_transparency=False)


async def _line(robot: AimRobot, x1: int, y1: int, x2: int, y2: int, width: int = 4) -> None:
    await robot.command("lcd_set_pen_width", width=width)
    await robot.command("lcd_set_pen_color", r=INK[0], g=INK[1], b=INK[2])
    await robot.command("lcd_draw_line", x1=x1, y1=y1, x2=x2, y2=y2)


async def _face(robot: AimRobot, expression: str) -> None:
    await robot.command("lcd_set_pen_width", width=3)
    await _circle(robot, CX, CY, R, FACE, INK)
    await robot.command("lcd_set_pen_width", width=1)
    eye_y, eye_dx = CY - 12, 15
    if expression == "excited":  # happy eyes: ^ ^
        for side in (-1, 1):
            await _line(robot, CX + side * eye_dx - 9, eye_y + 5, CX + side * eye_dx, eye_y - 5, 5)
            await _line(robot, CX + side * eye_dx, eye_y - 5, CX + side * eye_dx + 9, eye_y + 5, 5)
    elif expression == "wink":
        await _line(robot, CX - eye_dx - 7, eye_y, CX - eye_dx + 7, eye_y)
        await _circle(robot, CX + eye_dx, eye_y, 6, INK)
    elif expression == "focused":  # eyes under determined brows
        for side in (-1, 1):
            await _circle(robot, CX + side * eye_dx, eye_y + 2, 5, INK)
            await _line(robot, CX + side * (eye_dx + 9), eye_y - 10, CX + side * (eye_dx - 7), eye_y - 5)
    else:
        size = 8 if expression == "surprised" else 6
        for side in (-1, 1):
            await _circle(robot, CX + side * eye_dx, eye_y, size, INK)
    await robot.command("lcd_set_pen_width", width=1)
    if expression in ("happy", "wink", "excited"):  # a grin: the bottom half of a dark circle
        r = 22 if expression == "excited" else 17
        await _circle(robot, CX, CY + 8, r, INK)
        await _rect(robot, CX - r - 2, CY + 8 - r - 2, 2 * r + 4, r + 2, FACE)
    elif expression == "sad":  # a frown: the top half of one, lower down, and a tear
        await _circle(robot, CX, CY + 20, 13, INK)
        await _rect(robot, CX - 15, CY + 20, 30, 15, FACE)
        await _circle(robot, CX - eye_dx - 3, eye_y + 14, 4, (90, 170, 255))
    elif expression == "surprised":
        await _circle(robot, CX, CY + 18, 9, INK)
    else:  # focused: a straight mouth
        await _line(robot, CX - 13, CY + 20, CX + 13, CY + 20)


async def show_player_card(robot: AimRobot, name: str | None, team: str | None,
                           expression: str = "happy", caption: str | None = None) -> None:
    """Draw the card: team colour, the face with an expression, a caption on top, the name below."""
    if expression not in EXPRESSIONS:
        raise ValueError(f"expression must be one of {', '.join(EXPRESSIONS)}")
    bg = TEAM_RGB.get(team, TEAM_RGB[None])
    await robot.hide_emoji()  # an emoji would cover the card
    await robot.command("lcd_clear_screen", r=bg[0], g=bg[1], b=bg[2])
    await robot.command("lcd_set_fill_color", r=bg[0], g=bg[1], b=bg[2], b_transparency=True)
    await _face(robot, expression)
    if caption := (caption if caption is not None else CAPTIONS.get(expression)):
        await _text(robot, caption[:14], 52, WHITE)
    if name:
        await _text(robot, name[:16], 196, WHITE)
    await robot.set_led("all", bg)


_back_to_rest: dict[int, asyncio.Task] = {}  # per robot: the pending return to the resting face


async def react(robot: AimRobot, state, expression: str, caption: str | None = None, hold_s: float = REACTION_S) -> None:
    """Show a reaction on the player card, then go back to the resting face after hold_s seconds
    (0 = stay). The name and team are read from state (.player, .team) when it goes back, in case
    they changed meanwhile."""
    if (pending := _back_to_rest.pop(id(robot), None)) and not pending.done():
        pending.cancel()
    await show_player_card(robot, state.player, state.team, expression, caption)
    if expression != RESTING and hold_s > 0:
        async def back() -> None:
            await asyncio.sleep(hold_s)
            with contextlib.suppress(Exception):  # the robot may have gone away meanwhile
                await show_player_card(robot, state.player, state.team, RESTING)

        _back_to_rest[id(robot)] = asyncio.get_running_loop().create_task(back())
