"""Turn a recorded agent run into a short, visual-first video.

Inputs: the raw screen capture, the server's event log (ASCENDED_DEMO_EVENTS:
tool calls, pointer actions, where the viewport sits on screen, and the boxes
of elements a tool named) and the agent's final answer.

The video shows instead of telling. There are no captions: the eye follows the
pointer, so everything happens at the pointer or on the page itself.

* The pointer glides between the real click points; each click ripples.
* Phone and tablet checks become a device on a stage, morphing out of the
  desktop window and back.
* Dark mode is a circular reveal from the pointer. A screenshot is a shutter
  flash and a print; comparing two is a before/after slider.
* An audit sweeps the page, then outlines the real offending elements. Console
  and network reads open a devtools panel built from what the tool returned.
* Waiting is cut, tool time plays at 2x, and it ends on the answer.

Frames come from the capture, in order; overlays are drawn from the log.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
SPEED = 2.0             # source seconds per output second while a tool runs
GAP = 0.2               # output seconds left for each thinking gap
LEAD, TAIL = 0.1, 0.2   # source seconds kept before and after each tool call
INTRO = 0.5
END_SECONDS = 2.8
DEVICE_HEIGHT = 900
MORPH = 0.9
PACE = 1.0              # >1 gives every step and hold more screen time
DEVICE_CX = W / 2 + 70   # devices sit right of centre, leaving room for the terminal
FREEZE_LEAD = 0.35     # seconds a freeze starts before its tool call

FONT = "/usr/share/fonts/noto/NotoSans-Regular.ttf"
FONT_BOLD = "/usr/share/fonts/noto/NotoSans-Bold.ttf"
MONO = "/usr/share/fonts/noto/NotoSansMono-Regular.ttf"
MONO_BOLD = "/usr/share/fonts/noto/NotoSansMono-Bold.ttf"
UI = "/usr/share/fonts/TTF/DejaVuSans.ttf"
INK = (240, 241, 247)
DIM = (150, 154, 170)
ACCENT = (129, 140, 248)
SCAN = (56, 189, 248)
GREEN = (74, 222, 128)
RED = (248, 92, 92)
AMBER = (251, 191, 36)
INSPECT = (111, 168, 220)
IMPACT_COLOUR = {"critical": RED, "serious": RED, "moderate": AMBER, "minor": (250, 204, 21)}

_fonts: dict = {}


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def pop(t: float) -> float:
    """0 -> 1 with a small overshoot."""
    t = min(1.0, max(0.0, t))
    return 1 + 2.2 * (t - 1) ** 3 + 1.2 * (t - 1) ** 2


def lerp(a, b, k):
    return tuple(x + (y - x) * k for x, y in zip(a, b))


# ── Events ────────────────────────────────────────────────────────────────

@dataclass
class Beat:
    start: float
    end: float
    tool: str
    args: dict
    ok: bool = True
    text: str = ""
    boxes: list = field(default_factory=list)
    repeat: bool = False    # the same read as an earlier step: it gets less screen time
    image: str = ""         # the picture the tool returned (browser_screenshot), as the agent got it

    @property
    def read(self) -> str:
        return str(self.args.get("read") or "")

    @property
    def action(self) -> dict:
        action = self.args.get("action")
        return action if isinstance(action, dict) else {}


def load_events(path: Path, t0: float):
    beats: list[Beat] = []
    pointers: list[dict] = []
    geometry: list[tuple[float, dict]] = []
    boxes: list[dict] = []
    running: list[Beat] = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        t = event["t"] - t0
        kind = event.get("type")
        if kind == "tool" and event["phase"] == "start":
            beat = Beat(t, t, event["tool"], event.get("args") or {})
            beats.append(beat)
            running.append(beat)
        elif kind == "tool" and event["phase"] == "end":
            beat = next((b for b in running if b.tool == event["tool"]), None)
            if beat:
                running.remove(beat)
                beat.end, beat.ok, beat.text = t, bool(event.get("ok")), event.get("text", "")
                beat.image = event.get("image", "")
        elif kind == "pointer":
            pointers.append({**event, "t": t})
        elif kind == "geometry":
            geometry.append((t, event))
        elif kind == "boxes":
            boxes.append({**event, "t": t})
    seen = set()
    for beat in beats:
        if beat.tool == "browser_extract" and beat.read:
            beat.repeat = beat.read in seen
            seen.add(beat.read)
    for box in boxes:
        beat = next((b for b in beats if b.start - 0.05 <= box["t"] <= b.end + 1.0
                     and (box["kind"] != "field" or b.tool == "browser_act")), None)
        if beat:
            beat.boxes.append(box)
    return beats, pointers, geometry, boxes


def relevant_text(beat: Beat) -> str:
    match = re.search(r'"relevant_text": "((?:[^"\\]|\\.)*)"', beat.text)
    return json.loads(f'"{match.group(1)}"') if match else ""


def hold_for(beat: Beat) -> float:
    """Output seconds the page holds still after a beat, so its visual can land."""
    return _hold(beat) * PACE


def _hold(beat: Beat) -> float:
    if beat.repeat:
        return 0.4 if beat.boxes else 0.0
    if beat.tool == "browser_extract":
        if beat.read in {"console", "network"}:
            return 1.0
        if beat.read == "audit":
            return 1.2
        if (beat.args.get("selector") or beat.args.get("find")) and beat.boxes:
            return 0.8
    if beat.tool == "browser_screenshot":
        return 1.3 if beat.args.get("compare_with") else 0.45
    if beat.tool == "browser_viewport":
        if beat.args.get("action") in {"set", "restore"}:
            return 0.6
        if beat.args.get("color_scheme"):
            return 0.8
    return 0.0


ACTIVE_KINDS = {"click", "fill", "type", "select", "press", "check", "uncheck", "fill_form", "drag"}


def beat_rate(beat: Beat) -> float:
    """Source seconds per output second that keeps this step under its cap."""
    hands_on = beat.tool == "browser_act" and beat.action.get("kind") in ACTIVE_KINDS
    unseen = beat.tool in {"browser_evaluate", "browser_observe"} or (
        beat.tool == "browser_extract" and not beat.boxes and beat.read not in {"audit", "console", "network"})
    cap = 2.0 if hands_on else 0.45 if unseen or beat.repeat else 0.9
    return max(SPEED, (beat.end - beat.start + 0.5) / (cap * PACE))


class TimeMap:
    """Output time <-> source time: tools at SPEED, gaps cut to GAP, holds freeze the page."""

    def __init__(self, beats: list[Beat], start: float, end: float):
        spans: list[list[float]] = []
        for beat in beats:
            s0, s1 = max(start, beat.start - LEAD), min(end, beat.end + TAIL)
            if s1 <= s0:
                continue
            if spans and s0 <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], s1)
            else:
                spans.append([s0, s1])
        segs: list[list[float]] = []  # [src0, src1, out_len]
        cursor = start
        for s0, s1 in spans:
            if s0 > cursor:
                segs.append([cursor, s0, GAP])
            # Tool time plays at SPEED, but no single step outlasts its cap on
            # screen: long scrolls, reads and waits play faster.
            a = max(s0, cursor)
            while a < s1 - 1e-6:
                b = min(s1, a + 0.1)
                mid = (a + b) / 2
                rate = max([SPEED] + [beat_rate(beat) for beat in beats if beat.start - LEAD <= mid <= beat.end + TAIL])
                segs.append([a, b, (b - a) / rate])
                a = b
            cursor = s1
        for beat in beats:
            hold = hold_for(beat)
            at = beat.end + 0.1
            if hold <= 0 or not (start <= at < cursor):
                continue
            for i, (s0, s1, length) in enumerate(segs):
                if s0 <= at < s1:
                    k = (at - s0) / (s1 - s0)
                    segs[i:i + 1] = [[s0, at, length * k], [at, at, hold], [at, s1, length * (1 - k)]]
                    break
        self.segs = []
        out = 0.0
        for s0, s1, length in segs:
            if length > 0:
                self.segs.append((out, s0, s1, length))
                out += length
        self.duration = out
        self.source_end = cursor

    def source(self, out_t: float) -> float:
        for o0, s0, s1, length in self.segs:
            if out_t < o0 + length:
                return s0 + (s1 - s0) * max(0.0, out_t - o0) / length
        return self.source_end

    def output(self, src_t: float) -> float:
        for o0, s0, s1, length in self.segs:
            if s1 > s0 and s0 <= src_t <= s1:
                return o0 + (src_t - s0) / (s1 - s0) * length
            if src_t < s0:
                return o0
        return self.duration


# ── Drawing ───────────────────────────────────────────────────────────────

def stage() -> Image.Image:
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    glow = np.exp(-(((x - 0.5) * 1.6) ** 2 + ((y - 0.45) * 2.2) ** 2))
    r = 9 + 8 * x + 22 * glow
    g = 10 + 6 * y + 20 * glow
    b = 18 + 18 * (1 - y) + 48 * glow
    return Image.fromarray(np.dstack([r + 0 * x, g + 0 * x, b + 0 * x]).clip(0, 255).astype(np.uint8)).convert("RGBA")


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def cover(img: Image.Image, rect) -> Image.Image:
    """img scaled to cover rect (centred crop), sized to rect."""
    x0, y0, x1, y1 = rect
    w, h = max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))
    scale = max(w / img.width, h / img.height)
    sw, sh = max(w, int(math.ceil(img.width * scale))), max(h, int(math.ceil(img.height * scale)))
    big = img.resize((sw, sh), Image.BILINEAR)
    left, top = (sw - w) // 2, (sh - h) // 2
    return big.crop((left, top, left + w, top + h))


def window_view(img: Image.Image, rect) -> Image.Image:
    """img at its own scale in a window of rect's size, anchored top-left."""
    w, h = max(1, int(round(rect[2] - rect[0]))), max(1, int(round(rect[3] - rect[1])))
    img = img.convert("RGB")
    canvas = Image.new("RGB", (w, h), img.getpixel((img.width // 2, img.height - 2)))
    canvas.paste(img.crop((0, 0, min(w, img.width), min(h, img.height))), (0, 0))
    return canvas


def draw_device(frame: Image.Image, rect, screen: Image.Image, device: float, phone: bool) -> None:
    """screen in rect; with device > 0 it sits in a bezel on the stage."""
    x0, y0, x1, y1 = (int(round(v)) for v in rect)
    w, h = x1 - x0, y1 - y0
    radius = int((44 if phone else 26) * device)
    if device > 0.02:
        pad = int(16 * device)
        shadow = Image.new("RGBA", (w + 2 * pad + 160, h + 2 * pad + 160), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((80, 100, w + 2 * pad + 80, h + 2 * pad + 80),
                                                 radius=radius + pad, fill=(0, 0, 0, int(160 * device)))
        frame.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(30)), (x0 - pad - 80, y0 - pad - 80))
        bezel = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        d = ImageDraw.Draw(bezel)
        d.rounded_rectangle((0, 0, w + 2 * pad - 1, h + 2 * pad - 1), radius=radius + pad,
                            fill=(14, 14, 18, int(255 * device)), outline=(70, 72, 84, int(255 * device)), width=2)
        frame.alpha_composite(bezel, (x0 - pad, y0 - pad))
    if screen.size != (w, h):
        screen = screen.resize((w, h), Image.BILINEAR)
    frame.paste(screen.convert("RGB"), (x0, y0), rounded_mask((w, h), radius) if radius > 1 else None)
    if device > 0.5 and phone:
        a = int(255 * min(1, (device - 0.5) * 2))
        d = ImageDraw.Draw(frame)
        d.rounded_rectangle((x0 + w // 2 - 54, y0 + 10, x0 + w // 2 + 54, y0 + 40), radius=15, fill=(0, 0, 0, a))


def draw_pointer(frame: Image.Image, x: float, y: float, pressed: float, scale: float = 1.6) -> None:
    s = scale * (1 - 0.14 * pressed)
    shape = [(0, 0), (0, 34), (9, 26), (15, 40), (21, 37), (15, 24), (27, 24)]
    points = [(x + px * s, y + py * s) for px, py in shape]
    box = (int(x) - 20, int(y) - 20, int(x + 60 * s), int(y + 70 * s))
    layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon([(px - box[0] + 3, py - box[1] + 6) for px, py in points], fill=(0, 0, 0, 110))
    layer = layer.filter(ImageFilter.GaussianBlur(4))
    d = ImageDraw.Draw(layer)
    local = [(px - box[0], py - box[1]) for px, py in points]
    d.polygon(local, fill=(255, 255, 255, 255))
    d.line(local + [local[0]], fill=(12, 12, 18, 255), width=3, joint="curve")
    frame.alpha_composite(layer, (box[0], box[1]))


def draw_ripple(frame: Image.Image, x: float, y: float, age: float) -> None:
    if not 0 <= age <= 0.6:
        return
    d = ImageDraw.Draw(frame)
    k = ease_out(age / 0.6)
    radius = 12 + 58 * k
    alpha = int(230 * (1 - k))
    d.ellipse((x - radius, y - radius, x + radius, y + radius), outline=ACCENT + (alpha,), width=6)
    inner = 16 * (1 - k)
    d.ellipse((x - inner, y - inner, x + inner, y + inner), fill=ACCENT + (int(alpha * 0.7),))


def draw_box(layer: Image.Image, rect, colour, alpha: float, fill: int = 34, width: int = 4, radius: int = 6) -> None:
    x0, y0, x1, y1 = rect
    if x1 - x0 < 2 or y1 - y0 < 2 or alpha <= 0:
        return
    ImageDraw.Draw(layer).rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=colour + (int(fill * alpha),),
                                            outline=colour + (int(255 * alpha),), width=width)


def draw_icon(frame: Image.Image, kind: str, x: float, y: float, size: float, alpha: float) -> None:
    if size < 2 or alpha <= 0:
        return
    s = int(size)
    layer = Image.new("RGBA", (s * 2 + 8, s * 2 + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c = s + 4
    a = int(255 * alpha)
    d.ellipse((c - s, c - s, c + s, c + s), fill=(20, 22, 32, int(225 * alpha)))
    if kind == "moon":
        r = s * 0.55
        d.ellipse((c - r, c - r, c + r, c + r), fill=(226, 232, 255, a))
        d.ellipse((c - r + r * 0.55, c - r - r * 0.25, c + r + r * 0.55, c + r - r * 0.25), fill=(20, 22, 32, int(255 * alpha)))
    elif kind == "sun":
        r = s * 0.3
        d.ellipse((c - r, c - r, c + r, c + r), fill=(252, 211, 77, a))
        for i in range(8):
            ang = i * math.pi / 4
            d.line((c + math.cos(ang) * r * 1.45, c + math.sin(ang) * r * 1.45,
                    c + math.cos(ang) * r * 2.1, c + math.sin(ang) * r * 2.1), fill=(252, 211, 77, a), width=max(2, s // 9))
    frame.alpha_composite(layer, (int(x - c), int(y - c)))


def draw_count(frame: Image.Image, x: float, y: float, number: int, colour, k: float) -> None:
    if k <= 0:
        return
    r = 30 * pop(k)
    d = ImageDraw.Draw(frame)
    d.ellipse((x - r, y - r, x + r, y + r), fill=colour + (240,), outline=(255, 255, 255, 230), width=3)
    if r > 12:
        d.text((x, y + 1), str(number), font=font(FONT_BOLD, int(r * 1.05)), fill=(255, 255, 255, 255), anchor="mm")


# ── Devtools panel ────────────────────────────────────────────────────────

TABS = ["Inspector", "Console", "Debugger", "Network", "Style Editor", "Performance"]
TYPE_COLOUR = {"script": (233, 164, 71), "fetch": (155, 121, 232), "xhr": (155, 121, 232), "document": (79, 156, 245),
               "img": (79, 194, 140), "image": (79, 194, 140), "stylesheet": (230, 74, 139), "font": (120, 200, 220)}


def console_rows(beat: Beat) -> list[dict]:
    rows = []
    for line in relevant_text(beat).splitlines():
        m = re.match(r"\[\d+\]\s*(error|warning|warn|info|log|debug)?:?\s*(.*)", line.strip())
        if not m or not line.strip().startswith("["):
            continue
        level, msg = (m.group(1) or "log"), m.group(2)
        repeat = re.search(r"×(\d+)\s*$", msg)
        msg = re.sub(r"\s*×\d+\s*$", "", msg)
        rows.append({"level": "error" if level == "error" else "warning" if level.startswith("warn") else "log",
                     "text": msg, "repeat": repeat.group(1) if repeat else ""})
    return rows


def network_rows(beat: Beat) -> list[dict]:
    rows = []
    for line in relevant_text(beat).splitlines():
        m = re.match(r"\[\d+\]\s*(\w+)\s+(\S+)\s+→\s+(\S+)\s+(\w+)?\s*(\d+)?(?:ms)?", line.strip())
        if not m:
            continue
        method, url, status, kind, ms = m.groups()
        name = url.split("?")[0].rstrip("/").split("/")[-1] or url.split("/")[0]
        rows.append({"method": method, "name": name[:38], "status": status, "type": kind or "",
                     "ms": int(ms or 40), "third": "third-party" in line})
    return rows


def draw_devtools(frame: Image.Image, rect, tab: str, rows: list[dict], t: float, appear: float) -> None:
    """A Firefox-style devtools panel in rect; t = seconds since this tab's rows arrived."""
    x0, y0, x1, y1 = (int(v) for v in rect)
    w, h = x1 - x0, y1 - y0
    if w < 80 or h < 80 or appear <= 0:
        return
    panel = Image.new("RGBA", (w, h), (35, 35, 39, 250))
    d = ImageDraw.Draw(panel)
    d.rectangle((0, 0, w, 46), fill=(24, 24, 26, 255))
    d.line((0, 46, w, 46), fill=(56, 56, 61, 255), width=1)
    tx = 18
    for name in TABS:
        f = font(FONT, 21)
        tw = int(f.getlength(name))
        if tx + tw > w - 10:
            break
        active = name == tab
        d.text((tx, 23), name, font=f, fill=(255, 255, 255, 255) if active else (177, 177, 179, 255), anchor="lm")
        if active:
            d.rectangle((tx - 8, 43, tx + tw + 8, 46), fill=(10, 132, 255, 255))
        tx += tw + 34
    row_h = 38
    y = 54
    if tab == "Network":
        d.rectangle((0, 46, w, 46 + 34), fill=(30, 30, 33, 255))
        for label, cx in (("Status", 16), ("Method", 96), ("File", 196), ("Type", int(w * 0.52)), ("Waterfall", int(w * 0.66))):
            d.text((cx, 63), label, font=font(FONT, 18), fill=(150, 150, 156, 255), anchor="lm")
        y = 84
        total = sum(r["ms"] for r in rows[:40]) or 1
        offset = 0
        for i, row in enumerate(rows):
            if y + row_h > h:
                break
            k = ease_out((t - i * 0.035) / 0.35)
            if k <= 0:
                break
            ok = row["status"].startswith(("2", "3"))
            text_colour = (230, 230, 232, int(255 * k)) if ok else (255, 120, 120, int(255 * k))
            if i % 2:
                d.rectangle((0, y, w, y + row_h), fill=(39, 39, 43, 255))
            d.ellipse((16, y + 14, 26, y + 24), fill=((60, 200, 120) if ok else RED) + (int(255 * k),))
            d.text((34, y + row_h // 2), row["status"], font=font(MONO, 19), fill=text_colour, anchor="lm")
            d.text((96, y + row_h // 2), row["method"], font=font(MONO, 19), fill=text_colour, anchor="lm")
            name = row["name"]
            limit = max(8, int((w * 0.52 - 210) / 11))
            d.text((196, y + row_h // 2), name if len(name) <= limit else name[:limit - 1] + "…",
                   font=font(MONO, 19), fill=text_colour, anchor="lm")
            d.text((int(w * 0.52), y + row_h // 2), row["type"], font=font(MONO, 19),
                   fill=(170, 170, 176, int(255 * k)), anchor="lm")
            wx0, wx1 = int(w * 0.66), w - 16
            start = wx0 + (wx1 - wx0) * offset / total
            length = max(6, (wx1 - wx0) * row["ms"] / total * 3.2)
            colour = TYPE_COLOUR.get(row["type"], (150, 150, 160))
            grow = ease_out((t - i * 0.035 - 0.05) / 0.4)
            d.rounded_rectangle((start, y + 12, min(wx1, start + length * grow), y + row_h - 12), radius=4,
                                fill=colour + (230,))
            offset += row["ms"] * 0.55
            y += row_h
    else:
        for i, row in enumerate(rows):
            if y + row_h + 6 > h:
                break
            k = ease_out((t - i * 0.06) / 0.3)
            if k <= 0:
                break
            level = row["level"]
            bg = {"error": (78, 33, 40), "warning": (66, 56, 30)}.get(level, (35, 35, 39))
            fg = {"error": (255, 154, 162), "warning": (252, 226, 161)}.get(level, (220, 220, 224))
            d.rectangle((0, y, w, y + row_h + 4), fill=bg + (int(255 * k),))
            d.line((0, y + row_h + 4, w, y + row_h + 4), fill=(56, 56, 61, 255))
            cx, cy = 24, y + (row_h + 4) // 2
            if level == "warning":
                d.polygon([(cx, cy - 10), (cx - 11, cy + 9), (cx + 11, cy + 9)], fill=AMBER + (int(255 * k),))
            elif level == "error":
                d.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=RED + (int(255 * k),))
            text = row["text"]
            limit = max(10, int((w - 120) / 11.2))
            d.text((48, cy), text if len(text) <= limit else text[:limit - 1] + "…", font=font(MONO, 19),
                   fill=fg + (int(255 * k),), anchor="lm")
            if row["repeat"]:
                d.rounded_rectangle((w - 52, cy - 12, w - 16, cy + 12), radius=12, fill=(90, 90, 98, int(255 * k)))
                d.text((w - 34, cy), row["repeat"], font=font(FONT_BOLD, 16), fill=(255, 255, 255, int(255 * k)), anchor="mm")
            y += row_h + 5
    mask = rounded_mask((w, h), 14)
    if appear < 1:
        mask = mask.point(lambda v: int(v * appear))
    frame.paste(panel, (x0, y0), ImageChops.multiply(mask, panel.getchannel("A")))


# ── Terminal: the agent's real tool calls ─────────────────────────────────

CLAUDE = (217, 119, 87)


def _arg(value) -> str:
    if isinstance(value, str):
        return '"' + (value if len(value) <= 34 else value[:33] + "…") + '"'
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_arg(v)}" for k, v in value.items() if k not in {"reasoning", "expect", "until"}) + "}"
    text = json.dumps(value)
    return text if len(text) <= 34 else text[:33] + "…"


def call_line(beat: Beat) -> str:
    args = {k: v for k, v in beat.args.items() if k not in {"tab_id", "purpose", "label"}}
    if beat.tool == "browser_evaluate":
        args = {"function": "() => {…}"}
    return f"{beat.tool}(" + ", ".join(f"{k}: {_arg(v)}" for k, v in args.items()) + ")"


def result_line(beat: Beat) -> tuple[str, tuple]:
    text = beat.text
    if not beat.ok:
        error = re.search(r'"error": "((?:[^"\\]|\\.)*)"', text)
        return "Error: " + (json.loads(f'"{error.group(1)}"') if error else "the action did not complete"), RED
    page = re.search(r"\*\*page:\*\* ([^\n]+)", text)
    receipt = re.search(r'"receipt": \{"action": "(\w+)".*?"state": "(\w+)"', text)
    if beat.tool == "browser_act" and receipt:
        return f"{receipt.group(1)} verified · {receipt.group(2).replace('_', ' ')}", DIM
    if beat.tool in {"browser_open", "browser_act"} and page:
        return page.group(1), DIM
    if beat.tool == "browser_extract" and isinstance(beat.args.get("find"), str):
        needle = beat.args["find"]
        passages = [json.loads(f'"{m}"') for m in re.findall(r'\{"at": \d+, "text": "((?:[^"\\]|\\.)*)"', text)]
        hit = next((x for x in passages if needle.lower() in x.lower()), "")
        if hit:
            at = hit.lower().index(needle.lower())
            hit = " ".join(hit[max(0, at - 50):at + 60].split())
        return f'{len(passages)} matches · "…{hit}…"' if hit else f"{len(passages)} matches", DIM
    rel = [line.strip() for line in relevant_text(beat).splitlines() if line.strip()]
    if beat.read == "audit":
        summary = next((line for line in rel if line.startswith("Summary:")), rel[0] if rel else "")
        return summary.replace("Summary: ", ""), DIM
    if rel:
        return rel[0], DIM
    message = re.search(r'"message": "((?:[^"\\]|\\.)*)"', text)
    if message:
        return json.loads(f'"{message.group(1)}"').split(";")[0].split(". ")[0], DIM
    receipt = re.search(r'"receipt": \{"action": "(\w+)".*?"state": "(\w+)"', text)
    if receipt:
        return f"{receipt.group(1)} verified · {receipt.group(2).replace('_', ' ')}", DIM
    result = re.search(r'"result": (\{[^}]*\}|"[^"]*"|[\w.]+)', text)
    if result:
        return result.group(1), DIM
    capture = re.search(r"Viewport at capture: (\d+)x(\d+)", text)
    if capture:
        return f"image {capture.group(1)}×{capture.group(2)} attached", DIM
    page = re.search(r"\*\*page:\*\* ([^\n]+)", text)
    return (page.group(1) if page else "done"), DIM


def _fit(text: str, f: ImageFont.FreeTypeFont, width: float) -> str:
    if f.getlength(text) <= width:
        return text
    while text and f.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


def draw_terminal(frame: Image.Image, rect, entries: list[dict], o: float, real_seconds: float, alpha: float) -> None:
    """Claude Code's transcript of the tool calls so far: what really ran, and what came back."""
    x0, y0, x1, y1 = (int(v) for v in rect)
    w, h = x1 - x0, y1 - y0
    if w < 200 or alpha <= 0:
        return
    panel = Image.new("RGBA", (w, h), (13, 13, 17, 236))
    d = ImageDraw.Draw(panel)
    d.rectangle((0, 0, w, 44), fill=(24, 24, 29, 255))
    for i in range(6):  # Claude's asterisk mark
        ang = i * math.pi / 3
        d.line((26 - 8 * math.cos(ang), 22 - 8 * math.sin(ang), 26 + 8 * math.cos(ang), 22 + 8 * math.sin(ang)),
               fill=CLAUDE + (255,), width=3)
    d.text((44, 22), "Claude Code", font=font(FONT_BOLD, 19), fill=(236, 236, 240, 255), anchor="lm")
    minutes, seconds = divmod(int(real_seconds), 60)
    d.text((w - 18, 22), f"real time {minutes}:{seconds:02d}", font=font(MONO, 17), fill=DIM + (255,), anchor="rm")
    call_font, result_font = font(MONO, 18), font(MONO, 17)
    line_w = w - 60
    blocks = []
    for entry in entries:
        if o < entry["ob"]:
            continue
        done = o >= entry["oe"]
        blocks.append((entry, done))
    # Fill from the top like a terminal; once full, the oldest lines scroll away.
    heights = [58 if done else 32 for _, done in blocks]
    first = 0
    while first < len(blocks) and sum(heights[first:]) > h - 68:
        first += 1
    y = 56
    for (entry, done), block_h in zip(blocks[first:], heights[first:]):
        k = ease_out((o - entry["ob"]) / 0.2)
        a = int(255 * k)
        blink = 0.55 + 0.45 * math.sin(o * 10) if not done else 1.0
        dot = (GREEN if entry["ok"] else RED) if done else (200, 200, 210)
        d.ellipse((19, y + 9, 29, y + 19), fill=dot + (int(a * blink),))
        name, _, rest = entry["call"].partition("(")
        d.text((40, y + 14), name, font=font(MONO_BOLD, 18), fill=(240, 240, 244, a), anchor="lm")
        nx = 40 + call_font.getlength(name)
        d.text((nx, y + 14), _fit("(" + rest, call_font, line_w - (nx - 40)), font=call_font, fill=(170, 172, 184, a), anchor="lm")
        if done:
            ka = int(255 * ease_out((o - entry["oe"]) / 0.2))
            text, colour = entry["result"]
            d.line(((44, y + 33), (44, y + 43), (56, y + 43)), fill=DIM + (ka,), width=2)
            d.text((64, y + 42), _fit(text, result_font, line_w - 30), font=result_font, fill=colour + (ka,), anchor="lm")
        y += block_h
    mask = rounded_mask((w, h), 16)
    if alpha < 1:
        mask = mask.point(lambda v: int(v * alpha))
    frame.paste(panel, (x0, y0), ImageChops.multiply(mask, panel.getchannel("A")))
    ImageDraw.Draw(frame).rounded_rectangle((x0, y0, x1, y1), radius=16, outline=(60, 62, 74, int(255 * alpha)), width=2)


# ── Camera (desktop only) ─────────────────────────────────────────────────

class Camera:
    def __init__(self):
        self.box = np.array([0.0, 0.0, float(W), float(H)])

    def update(self, target: np.ndarray, smoothing: float = 0.08) -> None:
        self.box += (target - self.box) * smoothing

    def reset(self) -> None:
        self.box = np.array([0.0, 0.0, float(W), float(H)])

    def to_frame(self, x: float, y: float) -> tuple[float, float]:
        x0, y0, x1, y1 = self.box
        return (x - x0) * W / (x1 - x0), (y - y0) * H / (y1 - y0)

    def view(self, src: Image.Image) -> Image.Image:
        x0, y0, x1, y1 = self.box
        if x1 - x0 >= W - 0.5:
            return src
        return src.resize((W, H), Image.BILINEAR, box=(x0, y0, x1, y1))


FOLLOW_ZOOM = 1.35      # the desktop is shown this close, panning with the pointer
FOLLOW_LEAD = 0.5       # seconds the camera looks ahead along the pointer's path


# ── Render ────────────────────────────────────────────────────────────────

def short_answer(answer: str) -> str:
    match = re.search(r"ANSWER:\s*(.+)", answer)
    text = (match.group(1) if match else answer.strip().splitlines()[-1] if answer.strip() else "").strip()
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s*\([^)]*\)", "", text).strip(" .'\"")


def cut(raw: Path, events: Path, t0: float, answer: str, out: Path, speed: float = SPEED, pace: float = 1.0) -> None:
    global SPEED, PACE
    SPEED, PACE = speed, pace
    beats, pointers, geometry, _boxes = load_events(events, t0)
    if not beats or not geometry:
        raise SystemExit("event log has no tool calls or no geometry")
    start = max(0.0, geometry[0][0] - 0.25)
    timeline = TimeMap(beats, start=start, end=max(b.end for b in beats) + 1.2)
    total_out = timeline.duration + END_SECONDS

    def geo_at(src_t: float) -> dict:
        found = geometry[0][1]
        for t, g in geometry:
            if t <= src_t + 0.02:
                found = g
        return found

    def is_device(g: dict) -> bool:
        return g["width"] < 1000

    def device_rect(g: dict) -> tuple[float, float, float, float]:
        h = DEVICE_HEIGHT
        w = g["width"] * h / g["height"]
        x0, y0 = DEVICE_CX - w / 2, (H - h) / 2 + 8
        return (x0, y0, x0 + w, y0 + h)

    # Spans where the page shows a frozen frame: while a resize settles, and
    # while a colour scheme flips (it is then revealed).
    # The capture can run a little ahead of the log, so each freeze starts
    # just before its tool call.
    freezes: list[tuple[float, float]] = []
    for beat in beats:
        if beat.tool == "browser_viewport" and beat.args.get("action") in {"set", "restore"}:
            settled = next((t for t, _ in geometry if t >= beat.start), beat.end)
            freezes.append((beat.start - FREEZE_LEAD, settled))
        elif beat.tool == "browser_viewport" and beat.args.get("color_scheme"):
            freezes.append((beat.start - FREEZE_LEAD, beat.end + 0.02))
    freeze_of = {id(b): s for b, (s, _) in zip([b for b in beats if b.tool == "browser_viewport" and (
        b.args.get("action") in {"set", "restore"} or b.args.get("color_scheme"))], freezes)}
    snaps: dict[float, Image.Image | None] = {s: None for s, _ in freezes}

    # Pointer keypoints in viewport fractions: clicks, and fields typed into.
    keys: list[dict] = []
    for p in pointers:
        if p.get("kind") in {"pointer_down", "click", "tap"}:
            keys.append({"t": p["t"], "x": p["x"], "y": p["y"], "click": True})
    for beat in beats:
        for box in beat.boxes:
            if box["kind"] == "field" and box["items"]:
                b = box["items"][0]
                keys.append({"t": box["t"] + 0.05, "x": b["x"] + min(b["w"] * 0.5, 0.04), "y": b["y"] + b["h"] / 2,
                             "click": True})
    keys.sort(key=lambda k: k["t"])
    for k in keys:
        k["o"] = timeline.output(k["t"])

    def cursor_frac(o: float) -> tuple[float, float]:
        home = {"o": -1.0, "x": 0.55, "y": 0.72}
        prev = home
        for key in keys:
            if key["o"] <= o:
                prev = key
        following = next((k for k in keys if k["o"] > o), None)
        if not following:
            return prev["x"], prev["y"]
        dist = math.hypot((following["x"] - prev["x"]) * 16, (following["y"] - prev["y"]) * 9) / 16
        travel = min(0.85, max(0.38, 0.32 + dist * 0.9))
        depart = max(prev["o"] + 0.18, following["o"] - 0.1 - travel)
        if o < depart:
            return prev["x"], prev["y"]
        k = ease((o - depart) / travel)
        x = prev["x"] + (following["x"] - prev["x"]) * k
        y = prev["y"] + (following["y"] - prev["y"]) * k
        arc = 0.06 * dist * math.sin(math.pi * k)
        return x - (following["y"] - prev["y"]) * arc * 4, y + (following["x"] - prev["x"]) * arc * 4

    # Devtools panel sessions: console/network reads close together share one panel.
    panels: list[dict] = []
    for beat in beats:
        if beat.tool != "browser_extract" or beat.read not in {"console", "network"} or not beat.ok:
            continue
        rows = console_rows(beat) if beat.read == "console" else network_rows(beat)
        if not rows:
            rows = [{"level": "log", "text": "No messages", "repeat": ""}] if beat.read == "console" else []
        oe = timeline.output(beat.end)
        tab = {"tab": "Console" if beat.read == "console" else "Network", "rows": rows, "o": oe}
        if panels and oe - panels[-1]["close"] < 0.8:
            panels[-1]["tabs"].append(tab)
            panels[-1]["close"] = oe + hold_for(beat) + 0.35
        else:
            panels.append({"open": oe - 0.05, "close": oe + hold_for(beat) + 0.35, "tabs": [tab]})

    entries = [{"ob": timeline.output(b.start), "oe": timeline.output(b.end), "ok": b.ok, "call": call_line(b),
                "result": result_line(b)} for b in beats]

    decoder = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(raw), "-f", "rawvideo",
                                "-pix_fmt", "rgb24", "-r", str(FPS), "-"], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium",
                                "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                               stdin=subprocess.PIPE)
    frame_bytes = W * H * 3
    src_index, src_frame = -1, None

    def live(src_t: float) -> Image.Image:
        nonlocal src_index, src_frame
        want = max(0, int(src_t * FPS))
        while src_index < want:
            data = decoder.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            src_index += 1
            src_frame = Image.frombytes("RGB", (W, H), data)
            now = src_index / FPS
            for s in snaps:
                if snaps[s] is None and now >= s:
                    snaps[s] = src_frame
        return src_frame

    def source_at(src_t: float) -> tuple[Image.Image, Image.Image]:
        """(what the page shows, the live frame) at src_t."""
        frame = live(src_t)
        for s0, s1 in freezes:
            if s0 <= src_t < s1 and snaps.get(s0) is not None:
                return snaps[s0], frame
        return frame, frame

    def blank(img: Image.Image, g: dict) -> bool:
        crop = img.crop((g["x"], g["y"], g["x"] + g["width"], g["y"] + g["height"])).resize((48, 27))
        return float(np.asarray(crop).std()) < 5.0

    def screen_image(src: Image.Image, g: dict, camera: Camera) -> tuple[Image.Image, tuple]:
        if is_device(g):
            rect = device_rect(g)
            crop = src.crop((g["x"], g["y"], g["x"] + g["width"], g["y"] + g["height"]))
            return crop.resize((int(rect[2] - rect[0]), int(rect[3] - rect[1])), Image.LANCZOS), rect
        return camera.view(src), (0.0, 0.0, float(W), float(H))

    bg = stage()
    camera = Camera()
    last_key = None
    morph = None
    last_good: Image.Image | None = None
    fade = None
    prev_screen = None
    prev_rect = None
    prev_device = 0.0
    thumbs: list[dict] = []
    last_frame = None
    answer_text = short_answer(answer)

    total = int(total_out * FPS)
    for i in range(total):
        o = i / FPS
        if o >= timeline.duration:
            frame = end_card(last_frame, answer_text, (o - timeline.duration) / 0.5, o - timeline.duration)
            encoder.stdin.write(frame.convert("RGB").tobytes())
            continue
        src_t = timeline.source(o)
        shown, live_frame = source_at(src_t)
        g = geo_at(src_t)
        device = is_device(g)

        # Loading pages are blank: hold the last good page and fade in the new one.
        if blank(shown, g) and last_good is not None:
            shown = last_good
            fade = None
            holding = True
        else:
            holding = False
            if last_good is not None and getattr(cut, "_was_holding", False):
                fade = (o, prev_screen)
            last_good = shown
        cut._was_holding = holding

        # Camera: one steady zoom on the desktop that pans with the pointer
        # (looking ahead to where it is going), wide only at the start and end.
        if device:
            camera.reset()
        else:
            zoom = 1 + (FOLLOW_ZOOM - 1) * ease((o - INTRO - 0.2) / 0.8) * (1 - ease((o - timeline.duration + 0.9) / 0.8))
            ax, ay = cursor_frac(o + FOLLOW_LEAD)
            cx, cy = g["x"] + ax * g["width"], g["y"] + ay * g["height"]
            zw, zh = W / zoom, H / zoom
            x0 = min(max(0.0, cx - zw / 2), W - zw)
            y0 = min(max(0.0, cy - zh / 2), H - zh)
            camera.update(np.array([x0, y0, x0 + zw, y0 + zh]), 0.06)
        screen, rect = screen_image(shown, g, camera)
        if fade and o - fade[0] < 0.25 and fade[1] is not None and fade[1].size == screen.size:
            screen = Image.blend(fade[1].convert("RGB"), screen.convert("RGB"), ease((o - fade[0]) / 0.25))

        key = (g["width"], g["height"])
        if last_key is not None and key != last_key and prev_screen is not None:
            morph = {"o": o, "img": prev_screen, "rect": prev_rect, "device": prev_device}
        last_key = key

        def mapper(fx: float, fy: float, m=None) -> tuple[float, float]:
            if device:
                r = rect if m is None else m
                return r[0] + fx * (r[2] - r[0]), r[1] + fy * (r[3] - r[1])
            return camera.to_frame(g["x"] + fx * g["width"], g["y"] + fy * g["height"])

        frame = bg.copy()
        devicek = 1.0 if device else 0.0
        draw_rect = rect
        if morph and o - morph["o"] < MORPH:
            k = ease((o - morph["o"]) / MORPH)
            draw_rect = lerp(morph["rect"], rect, k)
            devicek = morph["device"] + (devicek - morph["device"]) * k
            # Like dragging a window edge: content keeps its size while the
            # frame reshapes, then the reflowed layout fades in.
            old = window_view(morph["img"], draw_rect)
            new = window_view(screen, draw_rect)
            mixed = Image.blend(old, new, ease((k - 0.5) / 0.25))
            draw_device(frame, draw_rect, mixed, devicek, phone=device and g["width"] < 600 or
                        (not device and morph["device"] > 0.5 and morph["rect"][2] - morph["rect"][0] < 600))
        elif device:
            draw_device(frame, rect, screen, 1.0, phone=g["width"] < 600)
        else:
            frame.paste(screen.convert("RGB"), (0, 0))
        if o < INTRO:
            k = ease_out(o / INTRO)
            scaled = frame.resize((int(W * (0.9 + 0.1 * k)), int(H * (0.9 + 0.1 * k))), Image.BILINEAR)
            intro = bg.copy()
            intro.paste(scaled, ((W - scaled.width) // 2, (H - scaled.height) // 2))
            frame = Image.blend(bg, intro, k)
        content = screen  # what the page shows now, before overlays
        in_morph = morph is not None and o - morph["o"] < MORPH

        def to_out(fx: float, fy: float) -> tuple[float, float]:
            if in_morph:
                return draw_rect[0] + fx * (draw_rect[2] - draw_rect[0]), draw_rect[1] + fy * (draw_rect[3] - draw_rect[1])
            return mapper(fx, fy)

        page_rect = (*to_out(0, 0), *to_out(1, 1))
        fx, fy = cursor_frac(o)
        px, py = to_out(min(0.97, max(0.01, fx)), min(0.97, max(0.01, fy)))
        px += 2.5 * math.sin(o * 1.7)
        py += 2.0 * math.cos(o * 1.3)

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for beat in beats:
            ob, oe = timeline.output(beat.start), timeline.output(beat.end)
            hold = hold_for(beat)
            # Fields being typed into glow while the tool runs.
            for box in beat.boxes:
                if box["kind"] == "field" and ob <= o <= oe + 0.25:
                    b = box["items"][0]
                    a = min(1.0, (o - ob) / 0.2) * (1 - max(0.0, (o - oe) / 0.25))
                    r = (*to_out(b["x"], b["y"]), *to_out(b["x"] + b["w"], b["y"] + b["h"]))
                    pad = 6
                    draw_box(overlay, (r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad), ACCENT, a, fill=18, width=5, radius=10)
            if beat.tool == "browser_extract" and beat.read == "audit":
                if ob <= o <= oe:  # the scan sweeps the page while the audit runs
                    phase = ((o - ob) / 0.8) % 1.0
                    y = page_rect[1] + (page_rect[3] - page_rect[1]) * phase
                    d = ImageDraw.Draw(overlay)
                    for j in range(40):
                        a = int(70 * (1 - j / 40))
                        d.line((page_rect[0], y - j * 3, page_rect[2], y - j * 3), fill=SCAN + (a,), width=3)
                    d.line((page_rect[0], y, page_rect[2], y), fill=SCAN + (255,), width=4)
                life = oe + hold + 0.9
                if oe <= o <= life + 0.3:
                    fade_k = 1 - max(0.0, (o - life) / 0.3)
                    items = [it for box in beat.boxes if box["kind"] == "audit" for it in box["items"]]
                    for j, it in enumerate(items):
                        k = (o - oe - j * 0.05) / 0.25
                        if k <= 0:
                            continue
                        s = pop(k)
                        r = (*to_out(it["x"], it["y"]), *to_out(it["x"] + it["w"], it["y"] + it["h"]))
                        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
                        hw, hh = (r[2] - r[0]) / 2 * (1.25 - 0.25 * s) + 4, (r[3] - r[1]) / 2 * (1.25 - 0.25 * s) + 4
                        pulse = 0.75 + 0.25 * math.sin((o - oe) * 9)
                        draw_box(overlay, (cx - hw, cy - hh, cx + hw, cy + hh),
                                 IMPACT_COLOUR.get(it["tag"], RED), min(1, k) * fade_k * pulse, fill=46, width=4)
            if beat.tool == "browser_extract" and beat.args.get("find"):
                # Found words get a highlighter stroke, left to right.
                if oe <= o <= oe + hold + 1.2:
                    fade_k = 1 - max(0.0, (o - oe - hold - 0.9) / 0.3)
                    d = ImageDraw.Draw(overlay)
                    for j, box in enumerate([it for b in beat.boxes if b["kind"] == "find" for it in b["items"]]):
                        k = ease_out((o - oe - j * 0.12) / 0.3)
                        if k <= 0:
                            continue
                        r = (*to_out(box["x"], box["y"]), *to_out(box["x"] + box["w"], box["y"] + box["h"]))
                        pad = (r[3] - r[1]) * 0.18
                        d.rounded_rectangle((r[0] - pad, r[1] - pad, r[0] - pad + (r[2] - r[0] + 2 * pad) * k, r[3] + pad),
                                            radius=4, fill=(253, 224, 71, int(120 * fade_k)))
            if beat.tool == "browser_extract" and beat.args.get("selector") and beat.read != "audit":
                if oe <= o <= oe + hold + 0.8:
                    fade_k = 1 - max(0.0, (o - oe - hold - 0.5) / 0.3)
                    for j, box in enumerate([it for b in beat.boxes if b["kind"] == "query" for it in b["items"]]):
                        k = min(1.0, (o - oe - j * 0.05) / 0.2)
                        r = (*to_out(box["x"], box["y"]), *to_out(box["x"] + box["w"], box["y"] + box["h"]))
                        draw_box(overlay, r, INSPECT, max(0.0, k) * fade_k, fill=90, width=2, radius=2)
        frame.alpha_composite(overlay)

        # Colour scheme flips are revealed in a circle growing from the pointer.
        for beat in beats:
            scheme = beat.args.get("color_scheme") if beat.tool == "browser_viewport" else None
            if not scheme:
                continue
            oe = timeline.output(beat.end)
            before_frame = snaps.get(freeze_of.get(id(beat)))
            if oe <= o <= oe + 0.75 and before_frame is not None:
                k = ease((o - oe) / 0.7)
                before, _ = screen_image(before_frame, g, camera)
                after, _ = screen_image(live_frame, g, camera)
                x0, y0, x1, y1 = (int(v) for v in rect)
                radius = k * math.hypot(max(px - x0, x1 - px), max(py - y0, y1 - py))
                mask = Image.new("L", after.size, 0)
                ImageDraw.Draw(mask).ellipse((px - x0 - radius, py - y0 - radius, px - x0 + radius, py - y0 + radius), fill=255)
                merged = Image.composite(after.convert("RGB"), before.convert("RGB"), mask)
                if device:
                    draw_device(frame, rect, merged, 1.0, phone=g["width"] < 600)
                else:
                    frame.paste(merged, (0, 0))
                ring = ImageDraw.Draw(frame)
                if 4 < radius:
                    ring.ellipse((px - radius, py - radius, px + radius, py + radius), outline=(255, 255, 255, int(160 * (1 - k))), width=4)
            if oe - 0.1 <= o <= oe + 1.1:
                k = (o - oe + 0.1) / 0.3
                alpha = 1 - max(0.0, (o - oe - 0.8) / 0.3)
                draw_icon(frame, "moon" if scheme == "dark" else "sun", px + 70, py - 50, 40 * pop(k), alpha)

        # Screenshots: a shutter flash, then a print flies to the side.
        for beat in beats:
            if beat.tool != "browser_screenshot" or not beat.ok:
                continue
            oe = timeline.output(beat.end)
            if not any(t["beat"] is beat for t in thumbs) and o >= oe:
                thumbs.append({"beat": beat, "img": returned_picture(beat) or content.convert("RGB").copy(),
                               "o": oe, "rect": rect,
                               "compare": bool(beat.args.get("compare_with")), "index": len(thumbs)})
            if oe <= o <= oe + 0.25:
                flash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                a = int(200 * (1 - (o - oe) / 0.25))
                ImageDraw.Draw(flash).rectangle(page_rect, fill=(255, 255, 255, a))
                frame.alpha_composite(flash)
        for thumb in thumbs:
            draw_thumb(frame, thumb, thumbs, o, device)

        # Devtools panel.
        for panel in panels:
            if not panel["open"] <= o <= panel["close"] + 0.3:
                continue
            appear = ease_out((o - panel["open"]) / 0.3) * (1 - ease((o - panel["close"]) / 0.3))
            tab = panel["tabs"][0]
            for candidate in panel["tabs"]:
                if candidate["o"] <= o:
                    tab = candidate
            if device:
                r = rect
                x0 = r[2] + 60
                prect = (x0 + 200 * (1 - appear), r[1] + 40, W - 50 + 200 * (1 - appear), r[3] - 40)
                if prect[2] - prect[0] < 420:
                    prect = (W * 0.42, H * 0.56 + 300 * (1 - appear), W - 30, H - 30 + 300 * (1 - appear))
            else:
                ph = H * 0.44
                prect = (30, H - ph - 20 + (ph + 40) * (1 - appear), W - 30, H - 20 + (ph + 40) * (1 - appear))
            draw_devtools(frame, prect, tab["tab"], tab["rows"], o - tab["o"], appear)

        # The agent's own transcript: beside a device, or in a corner of the desktop.
        talpha = ease((o - 0.3) / 0.4)
        if in_morph:  # appear in the new place once the window has taken its new shape
            talpha *= ease(((o - morph["o"]) / MORPH - 0.6) / 0.4)
        if device:
            tw = min(640, int(rect[0] - 90))
            trect = (40, 80, 40 + tw, 760)
        else:
            trect = (W - 700, H - 270, W - 30, H - 30)
        draw_terminal(frame, trect, entries, o, max(0.0, src_t - beats[0].start), talpha)

        # The pointer, its clicks, and counts that pop next to it.
        for key in keys:
            if key["click"]:
                draw_ripple(frame, *to_out(key["x"], key["y"]), o - key["o"])
        for beat in beats:
            if beat.tool == "browser_extract" and beat.read == "audit":
                oe = timeline.output(beat.end)
                life = oe + hold_for(beat) + 0.9
                count = re.search(r'"violations": (\d+)', beat.text)
                if count and oe + 0.15 <= o <= life + 0.2:
                    k = (o - oe - 0.15) / 0.3
                    draw_count(frame, px + 74, py - 30, int(count.group(1)), RED, k * (1 - max(0.0, (o - life) / 0.2)))
        pressed = max([0.0] + [1 - abs(o - k["o"]) / 0.12 for k in keys if k["click"]])
        draw_pointer(frame, px, py, max(0.0, pressed), 1.6 if not device else 1.35)

        encoder.stdin.write(frame.convert("RGB").tobytes())
        prev_screen, prev_rect, prev_device = content, (rect if not in_morph else draw_rect), devicek
        last_frame = frame
    encoder.stdin.close()
    encoder.wait()
    decoder.kill()


def returned_picture(beat: Beat) -> Image.Image | None:
    """The picture the tool returned (masks and all); older logs only have the screen."""
    try:
        return Image.open(beat.image).convert("RGB") if beat.image else None
    except OSError:
        return None


def draw_thumb(frame: Image.Image, thumb: dict, thumbs: list[dict], o: float, device: bool) -> None:
    age = o - thumb["o"]
    if age < 0.05:
        return
    if thumb["compare"]:
        before = next((t for t in reversed(thumbs[:thumb["index"]]) if not t["compare"]), None)
        hold = hold_for(thumb["beat"])
        if before is None or age > hold + 0.45:
            return
        appear = ease_out((age - 0.05) / 0.3) * (1 - ease((age - hold - 0.1) / 0.35))
        ch = 820
        cw = int(ch * thumb["img"].width / thumb["img"].height)
        if cw > W - 200:
            cw = W - 200
            ch = int(cw * thumb["img"].height / thumb["img"].width)
        a, b = before["img"].resize((cw, ch), Image.LANCZOS), thumb["img"].resize((cw, ch), Image.LANCZOS)
        split = 0.5 + 0.38 * math.sin(min(1.0, max(0.0, (age - 0.3) / hold)) * math.pi * 1.5)
        card = b.copy()
        card.paste(a.crop((0, 0, int(cw * split), ch)), (0, 0))
        dim = Image.new("RGBA", (W, H), (6, 7, 12, int(150 * appear)))
        frame.alpha_composite(dim)
        scale = 0.85 + 0.15 * appear
        card = card.resize((int(cw * scale), int(ch * scale)), Image.BILINEAR)
        x0, y0 = (W - card.width) // 2, (H - card.height) // 2
        mask = rounded_mask(card.size, 18).point(lambda v: int(v * appear))
        frame.paste(card, (x0, y0), mask)
        d = ImageDraw.Draw(frame)
        sx = x0 + card.width * split
        d.line((sx, y0, sx, y0 + card.height), fill=(255, 255, 255, int(255 * appear)), width=5)
        d.ellipse((sx - 22, y0 + card.height / 2 - 22, sx + 22, y0 + card.height / 2 + 22),
                  fill=(255, 255, 255, int(255 * appear)))
        d.polygon([(sx - 12, y0 + card.height / 2), (sx - 4, y0 + card.height / 2 - 8), (sx - 4, y0 + card.height / 2 + 8)],
                  fill=(20, 20, 26, int(255 * appear)))
        d.polygon([(sx + 12, y0 + card.height / 2), (sx + 4, y0 + card.height / 2 - 8), (sx + 4, y0 + card.height / 2 + 8)],
                  fill=(20, 20, 26, int(255 * appear)))
        return
    # A print: flies from the page to a stack at the left (device) or bottom-left (desktop).
    plain = [t for t in thumbs if not t["compare"]]
    slot = plain.index(thumb)
    tw = 230 if device else 300
    th = int(tw * thumb["img"].height / thumb["img"].width)
    if th > 380:
        th = 380
        tw = int(th * thumb["img"].width / thumb["img"].height)
    if device:
        tw = 150
        th = int(tw * thumb["img"].height / thumb["img"].width)
        if th > 240:
            th = 240
            tw = int(th * thumb["img"].width / thumb["img"].height)
        tx, ty = 50 + (slot % 5) * 95, 800
        life = 99.0
    else:
        tx, ty = 40, H - th - 40
        life = 1.8
    if age > life + 0.3:
        return
    k = ease((age - 0.05) / 0.45)
    r0 = thumb["rect"]
    x = r0[0] + (tx - r0[0]) * k
    y = r0[1] + (ty - r0[1]) * k
    w = (r0[2] - r0[0]) + (tw - (r0[2] - r0[0])) * k
    h = (r0[3] - r0[1]) + (th - (r0[3] - r0[1])) * k
    alpha = 1 - ease((age - life) / 0.3)
    border = 10
    card = Image.new("RGBA", (int(w) + 2 * border, int(h) + 2 * border), (250, 250, 252, int(255 * alpha)))
    card.paste(thumb["img"].resize((int(w), int(h)), Image.BILINEAR), (border, border))
    angle = (-4 + 4 * (slot % 3)) * k
    card = card.rotate(angle, expand=True, resample=Image.BICUBIC)
    shadow = Image.new("RGBA", (card.width + 40, card.height + 40), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, int(120 * alpha)), (20, 26, card.width + 20, card.height + 26), card.getchannel("A"))
    frame.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)), (int(x) - 20, int(y) - 20))
    if alpha < 1:
        card.putalpha(card.getchannel("A").point(lambda v: int(v * alpha)))
    frame.alpha_composite(card, (int(x), int(y)))


def end_card(frame: Image.Image, answer: str, appear: float, age: float) -> Image.Image:
    appear = ease(appear)
    base = frame.convert("RGB").filter(ImageFilter.GaussianBlur(12 * appear)).convert("RGBA")
    base.alpha_composite(Image.new("RGBA", frame.size, (7, 8, 13, int(195 * appear))))
    d = ImageDraw.Draw(base)
    # A check that draws itself.
    cx, cy, r = W // 2, H // 2 - 170, 62
    k = ease_out((age - 0.1) / 0.5)
    if k > 0:
        d.arc((cx - r, cy - r, cx + r, cy + r), -90, -90 + 360 * k, fill=GREEN + (255,), width=10)
    k2 = ease_out((age - 0.45) / 0.3)
    if k2 > 0:
        p1, p2, p3 = (cx - 28, cy + 2), (cx - 8, cy + 24), (cx + 30, cy - 20)
        mid = lerp(p1, p2, min(1, k2 * 2))
        d.line((p1, mid), fill=GREEN + (255,), width=12)
        if k2 > 0.5:
            d.line((p2, lerp(p2, p3, (k2 - 0.5) * 2)), fill=GREEN + (255,), width=12)
    a = int(255 * ease((age - 0.35) / 0.35))
    size = 60 if len(answer) <= 76 else 48
    lines = textwrap.wrap(answer, 38 if size == 60 else 50)[:3]
    for n, line in enumerate(lines):
        d.text((W // 2, H // 2 - 30 + n * (size + 12)), line, font=font(FONT_BOLD, size), fill=INK + (a,), anchor="mm")
    a2 = int(255 * ease((age - 0.8) / 0.4))
    d.text((W // 2, H // 2 + 190), "claude mcp add ascended-browser -- uvx ascended-browser",
           font=font(MONO, 38), fill=ACCENT + (a2,), anchor="mm")
    return base
