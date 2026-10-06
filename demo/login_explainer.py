"""Cut the saved-login demo into a short explainer (GIF + MP4).

    python demo/login_explainer.py --raw videos/raw --out videos/

Made only from what demo/login_demo.py recorded: the CLI's own output, frames
of the real screen, the pictures browser_screenshot returned to the agent, the
exact text the agent got back, and its final ANSWER line. Every quoted string
is looked up in the event log and the cut stops if one is missing, so the
explainer cannot claim more than the run showed.

1. You save a login once (the password prompt shows nothing).
2. The agent calls browser_login; the vault fills the form.
3. Your screen shows the account; the agent's results and pictures do not.
4. The agent's own answer.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cut import ACCENT, DIM, FONT, FONT_BOLD, GREEN, INK, MONO, font, short_answer  # noqa: E402

W, H, FPS = 1280, 720, 12
BG = (14, 15, 22)
CARD = (27, 29, 40)
TERMINAL = (10, 11, 16)
MAGENTA = (255, 0, 255)
# The page area both sides show: the same region of the real screen (below the
# browser's own toolbar) and of the picture the agent got.
CROP_X, CROP_TOP, CROP_W, CROP_H = 560, 70, 800, 440
CARD_H = 44 + int(556 * CROP_H / CROP_W) + 14


def load(raw: Path, name: str) -> tuple[dict, list[dict]]:
    meta = json.loads((raw / f"{name}.meta.json").read_text())
    events = [json.loads(line) for line in (raw / f"{name}.events.jsonl").read_text().splitlines()]
    for event in events:
        event["t"] -= meta["t0"]
    return meta, [e for e in events if e.get("type") == "tool"]


def find(events: list[dict], tool: str, pattern: str) -> str:
    """The first match of pattern in a result of tool; stops the cut if the run never said it."""
    for event in events:
        if event["tool"] == tool and event["phase"] == "end":
            match = re.search(pattern, event.get("text", ""))
            if match:
                return match.group(0)
    raise SystemExit(f"the run never returned {pattern!r} from {tool}; not drawing it")


def frame_at(video: Path, t: float, work: Path) -> Image.Image:
    target = work / f"screen-{t:.2f}.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                    "-frames:v", "1", str(target)], check=True)
    return Image.open(target).convert("RGB")


def page_area(image: Image.Image, toolbar: int) -> Image.Image:
    top = toolbar + CROP_TOP
    return image.crop((CROP_X, top, CROP_X + CROP_W, top + CROP_H))


def ease(k: float) -> float:
    k = max(0.0, min(1.0, k))
    return k * k * (3 - 2 * k)


def header(d: ImageDraw.ImageDraw, step: str, text: str, k: float) -> None:
    a = int(255 * ease(k * 3))
    d.ellipse((48, 34, 92, 78), fill=(*ACCENT, a))
    d.text((70, 56), step, font=font(FONT_BOLD, 24), fill=(255, 255, 255, a), anchor="mm")
    d.text((110, 56), text, font=font(FONT_BOLD, 30), fill=(*INK, a), anchor="lm")


def card(frame: Image.Image, image: Image.Image, box: tuple[int, int, int, int], label: str, k: float) -> None:
    if k <= 0:
        return
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((x0, y0, x1, y1), 16, fill=(*CARD, 255))
    d.text((x0 + 20, y0 + 22), label, font=font(FONT_BOLD, 20), fill=(*INK, 255), anchor="lm")
    inner = image.resize((x1 - x0 - 24, int((x1 - x0 - 24) * image.height / image.width)), Image.LANCZOS)
    layer.paste(inner, (x0 + 12, y0 + 44))
    alpha = layer.split()[3].point(lambda v: int(v * ease(k)))
    layer.putalpha(alpha)
    frame.alpha_composite(layer, (0, int(24 * (1 - ease(k)))))


def snippets(frame: Image.Image, lines: list[tuple[str, str]], box: tuple[int, int, int, int], k: float) -> None:
    if k <= 0:
        return
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, 14, fill=(*TERMINAL, 255))
    y = y0 + 18
    for caption, value in lines:
        d.text((x0 + 18, y), caption, font=font(FONT, 16), fill=(*DIM, 255))
        d.text((x0 + 18, y + 22), value, font=font(MONO, 18), fill=(*GREEN, 255))
        y += 58
    layer.putalpha(layer.split()[3].point(lambda v: int(v * ease(k))))
    frame.alpha_composite(layer)


def terminal_scene(lines: list[tuple[str, float, float]], t: float) -> Image.Image:
    """lines: (text, when it starts, how long it types)."""
    frame = Image.new("RGBA", (W, H), (*BG, 255))
    d = ImageDraw.Draw(frame)
    header(d, "1", "Save a login once", t)
    d.rounded_rectangle((80, 150, W - 80, 450), 18, fill=(*TERMINAL, 255))
    for i, color in enumerate(((248, 92, 92), (251, 191, 36), (74, 222, 128))):
        d.ellipse((104 + i * 26, 172, 120 + i * 26, 188), fill=color)
    y = 230
    for text, start, typing in lines:
        if t < start:
            break
        shown = text if typing <= 0 else text[:int(len(text) * min(1.0, (t - start) / typing))]
        d.text((112, y), shown, font=font(MONO, 21), fill=(*INK, 255))
        y += 40
    d.text((W // 2, 510), "The password is typed into a hidden prompt (or stdin), never on the command line.",
           font=font(FONT, 22), fill=(*DIM, int(255 * ease((t - 2.6) * 2))), anchor="mm")
    return frame


def split_scene(step: str, title: str, left: Image.Image, right: Image.Image, lines: list[tuple[str, str]],
                t: float, left_before: Image.Image | None = None, swap_at: float = 0.0) -> Image.Image:
    frame = Image.new("RGBA", (W, H), (*BG, 255))
    header(ImageDraw.Draw(frame), step, title, t)
    shown = left
    if left_before is not None:
        k = ease((t - swap_at) / 0.4)
        shown = Image.blend(left_before, left, k) if k < 1 else left
    card(frame, shown, (48, 104, 628, 104 + CARD_H), "Your screen", t * 2)
    card(frame, right, (652, 104, W - 48, 104 + CARD_H), "What the agent gets", (t - 0.9) * 2)
    top = 104 + CARD_H + 16
    snippets(frame, lines, (48, top, W - 48, top + 22 + 58 * len(lines)), (t - 1.6) * 2)
    return frame


def end_scene(answer: str, backdrop: Image.Image, t: float) -> Image.Image:
    frame = backdrop.convert("RGB").filter(ImageFilter.GaussianBlur(14)).convert("RGBA")
    frame.alpha_composite(Image.new("RGBA", (W, H), (*BG, 200)))
    d = ImageDraw.Draw(frame)
    a = int(255 * ease(t * 2.5))
    d.text((W // 2, 200), "The agent's own answer", font=font(FONT, 24), fill=(*DIM, a), anchor="mm")
    d.text((W // 2, 300), f"“{answer}”", font=font(FONT_BOLD, 40), fill=(*INK, a), anchor="mm")
    d.text((W // 2, 470), "ascended-browser login add <site>", font=font(MONO, 30), fill=(*ACCENT, a), anchor="mm")
    d.text((W // 2, 520), "then ask your agent to sign in", font=font(FONT, 22), fill=(*DIM, a), anchor="mm")
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--raw", type=Path, default=Path("videos/raw"))
    parser.add_argument("--name", default="login")
    parser.add_argument("--out", type=Path, default=Path("videos"))
    args = parser.parse_args()
    meta, events = load(args.raw, args.name)
    video = args.raw / f"{args.name}.mp4"
    starts = {}
    for event in events:
        starts.setdefault((event["tool"], event["phase"]), []).append(event)
    login_start = starts[("browser_login", "start")][0]["t"]
    login_end = starts[("browser_login", "end")][0]["t"]
    shots = [e for e in starts[("browser_screenshot", "end")] if e.get("image")]
    if len(shots) < 2:
        raise SystemExit("the run needs a screenshot of the filled form and one of the dashboard")
    work = Path(tempfile.mkdtemp(prefix="login-explainer-"))
    toolbar = 1080 - Image.open(shots[0]["image"]).height  # the browser's own bar above the page
    no_toolbar = 0
    empty = page_area(frame_at(video, login_start - 0.3, work), toolbar)
    filled = page_area(frame_at(video, max(login_end + 0.8, shots[0]["t"] - 0.6), work), toolbar)
    dashboard = page_area(frame_at(video, shots[1]["t"] - 0.6, work), toolbar)
    agent_form = page_area(Image.open(shots[0]["image"]).convert("RGB"), no_toolbar)
    agent_dashboard = page_area(Image.open(shots[1]["image"]).convert("RGB"), no_toolbar)

    saved = next((line for line in meta.get("setup_output", "").splitlines() if line.startswith("Saved ")), "")
    if not saved:
        raise SystemExit("the recording has no CLI output (record it again with demo/login_demo.py)")
    site = re.search(r" for (\S+?)\.?$", saved).group(1)
    username = re.search(r"--username (\S+)", meta.get("setup", ""))
    command = f"$ ascended-browser login add {site} --name \"Acme Cloud\""
    command2 = f"      --username {username.group(1) if username else '<you>'}"
    label = find(events, "browser_open", r'"label": "[^"]*\[redacted\][^"]*"')
    password = find(events, "browser_open", r'"type": "password", "value": "\[masked\]"')
    filled_status = find(events, "browser_login", r'"status": "filled"')
    signed_in = find(events, "browser_act", r'"text": "Signed in as\\n\[redacted\]"')
    answer = short_answer(meta.get("answer", ""))

    frames = work / "frames"
    frames.mkdir()
    count = 0

    def emit(seconds: float, draw) -> None:
        nonlocal count
        for i in range(int(seconds * FPS)):
            draw(i / FPS).convert("RGB").save(frames / f"{count:05d}.png")
            count += 1

    terminal = [(command, 0.3, 0.9), (command2, 1.2, 0.5), ("Password: ", 1.9, 0), (saved, 2.5, 0)]
    emit(4.2, lambda t: terminal_scene(terminal, t))
    emit(5.0, lambda t: split_scene(
        "2", "The agent calls browser_login: the vault fills the form", filled, agent_form,
        [("browser_open: the saved login it can use", label), ("the password field, as the agent reads it", password),
         ("browser_login(submit: false)", filled_status)], t, left_before=empty, swap_at=0.5))
    emit(5.5, lambda t: split_scene(
        "3", "Signed in. The page shows the account; the agent never does", dashboard, agent_dashboard,
        [("the dashboard text, as the agent reads it", signed_in.replace("\\n", " ")),
         ("its screenshot", "account name and email painted over before the picture reaches the agent")], t))
    last = split_scene("3", "Signed in. The page shows the account; the agent never does", dashboard,
                       agent_dashboard, [], 5.0)
    emit(3.5, lambda t: end_scene(answer, last, t))

    args.out.mkdir(parents=True, exist_ok=True)
    mp4, gif = args.out / f"{args.name}.mp4", args.out / f"{args.name}.gif"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(frames / "%05d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(mp4)], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(frames / "%05d.png"),
                    "-vf", "scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];"
                           "[b][p]paletteuse=dither=bayer:bayer_scale=4", str(gif)], check=True)
    shutil.rmtree(work, ignore_errors=True)
    print(mp4, gif)


if __name__ == "__main__":
    main()
