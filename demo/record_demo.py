"""Record a real agent using ascended-browser, then cut it into a short video.

    python demo/record_demo.py --name dev --task "..." --out videos/

Claude Code runs the task with only this MCP server attached. The browser is
visible on a private 1920x1080 display that ffmpeg records (without the X
pointer, which Playwright never moves), and the server logs every tool call,
pointer action and the boxes of elements a tool named (ASCENDED_DEMO_EVENTS).
demo/cut.py turns capture + log into the video.
Raw captures and logs are kept under <out>/raw so a cut can be redone with
--recompose.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cut import H, W, cut  # noqa: E402


def run_agent(task: str, env: dict, work: Path, model: str) -> str:
    config = work / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"ascended-browser": {
        "command": os.environ.get("ASCENDED_BROWSER_BIN", "ascended-browser"), "env": env}}}))
    cmd = ["claude", "-p", task, "--mcp-config", str(config), "--strict-mcp-config",
           "--allowedTools", "mcp__ascended-browser", "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    out = subprocess.run(cmd, cwd=work, env={**os.environ, "PWD": str(work)}, capture_output=True, text=True,
                         timeout=1200).stdout
    answer = ""
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            answer = str(event.get("result") or "")
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--task", default="")
    parser.add_argument("--out", type=Path, default=Path("videos"))
    parser.add_argument("--model", default="")
    parser.add_argument("--display", default=":79")
    parser.add_argument("--recompose", action="store_true")
    parser.add_argument("--speed", type=float, default=2.0, help="playback speed while tools run")
    parser.add_argument("--pace", type=float, default=1.0, help=">1 gives each step and its visual more time")
    parser.add_argument("--setup", default="",
                        help="shell command run before the agent with its environment (e.g. saving a demo login)")
    args = parser.parse_args()
    raw_dir = args.out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw, events, meta = (raw_dir / f"{args.name}.mp4", raw_dir / f"{args.name}.events.jsonl",
                         raw_dir / f"{args.name}.meta.json")

    if not args.recompose:
        work = Path(tempfile.mkdtemp(prefix=f"demo-{args.name}-", dir="/var/tmp"))
        events.unlink(missing_ok=True)
        xvfb = subprocess.Popen(["Xvfb", args.display, "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.0)
        ffmpeg = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab", "-framerate", "30",
                                   "-video_size", f"{W}x{H}", "-draw_mouse", "0", "-i", args.display,
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", str(raw)],
                                  stdin=subprocess.PIPE)
        t0 = time.time()
        env = {
            "DISPLAY": args.display, "ASCENDED_BROWSER_WINDOW": "show",
            "ASCENDED_BROWSER_WINDOW_SIZE": f"{W}x{H}", "ASCENDED_DEMO_EVENTS": str(events),
            "ASCENDED_DATA_DIR": str(work / "data"),
        }
        setup_output = ""
        try:
            if args.setup:
                setup_output = subprocess.run(args.setup, shell=True, check=True, capture_output=True, text=True,
                                              env={**os.environ, **env}).stdout
                print(setup_output, end="")
            answer = run_agent(args.task, env, work, args.model)
            time.sleep(1.0)
        finally:
            ffmpeg.communicate(b"q", timeout=60)
            xvfb.terminate()
        meta.write_text(json.dumps({"task": args.task, "t0": t0, "answer": answer, "x_pointer": False,
                                    "setup": args.setup, "setup_output": setup_output}, indent=1))
    saved = json.loads(meta.read_text())
    out = args.out / f"{args.name}.mp4"
    cut(raw, events, saved["t0"], saved["answer"], out, speed=args.speed, pace=args.pace)
    print(out)


if __name__ == "__main__":
    main()
