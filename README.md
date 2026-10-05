<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/AceAtDev/ascended-browser/raw/main/docs/assets/ascended-browser-dark.png">
    <img src="https://github.com/AceAtDev/ascended-browser/raw/main/docs/assets/ascended-browser-light.png" alt="ascended-browser icon" width="96" height="96">
  </picture>
</p>
<h1 align="center">ascended-browser</h1>

<p align="center"><strong>A real browser for your AI agent. One command to add.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/ascended-browser/"><img src="https://img.shields.io/pypi/v/ascended-browser" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/ascended-browser"><img src="https://img.shields.io/npm/v/ascended-browser" alt="npm"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
</p>

A real browser for AI agents, as an MCP server. Your agent opens pages, reads
them, and acts on them through **verified actions**: it fills a whole form in
one call, picks "Mrs." from a custom React dropdown by name, and is told what
the page did in response, instead of clicking coordinates and hoping.

It is the browser from Ascended, packaged on its own:
[Camoufox](https://camoufox.com) (a hardened Firefox that looks like a
person's browser to the sites it visits) behind the same tool dispatcher, page
reading and result formatting Ascended's own agent uses.

## See it work

Three real runs of Claude Code with only this server attached, on live sites.
The panel on the side is Claude Code's own transcript: every tool call it made
and what came back, errors included, with the real elapsed time. Waiting is
cut and tool time plays faster; the pointer, device frames, devtools panels
and outlines are drawn afterwards from the server's event log
(`demo/record_demo.py`), so they sit on what the agent really touched and read.
The full unedited screen recordings are in [`videos/unedited/`](videos/unedited/)
(the browser is driven through Playwright, so no OS pointer appears in them).

<a href="https://github.com/AceAtDev/ascended-browser/blob/main/videos/dev-react.mp4"><img src="https://github.com/AceAtDev/ascended-browser/raw/main/videos/dev-react.gif" alt="Front-end QA. react.dev on a phone and a tablet, dark/light, before/after screenshots, an audit outlining the real offending elements, console and network." width="760"></a>

**Front-end QA.** react.dev on a phone and a tablet, dark/light, before/after screenshots, an audit outlining the real offending elements, console and network.

<a href="https://github.com/AceAtDev/ascended-browser/blob/main/videos/browse.mp4"><img src="https://github.com/AceAtDev/ascended-browser/raw/main/videos/browse.gif" alt="Browsing. Types into Wikipedia's search, finds a fact in the article, then searches YouTube and plays the first video." width="760"></a>

**Browsing.** Types into Wikipedia's search, finds a fact in the article, then searches YouTube and plays the first video.

<a href="https://github.com/AceAtDev/ascended-browser/blob/main/videos/booking.mp4"><img src="https://github.com/AceAtDev/ascended-browser/raw/main/videos/booking.gif" alt="Real forms. booking.com: popup, destination autocomplete, date picker, search, sort by price." width="760"></a>

**Real forms.** booking.com: popup, destination autocomplete, date picker, search, sort by price.

Click any clip for the full-quality MP4.

## Install

From Python (3.11 or newer) or from npm; both run the same server.

```bash
uvx ascended-browser doctor          # checks the machine; downloads nothing
uvx ascended-browser fetch           # downloads the Camoufox browser (otherwise on first use)

npx -y ascended-browser doctor       # the same, from npm
```

The npm package is a small launcher: it runs the Python package with `uvx`
when [uv](https://docs.astral.sh/uv/) is installed (uv brings its own Python),
else `pipx`, else a private venv made with your Python 3.11+. Use whichever
command you prefer in the configs below (`npx -y ascended-browser` in place of
`uvx ascended-browser`).

Linux: install `xvfb` to keep the browser on a virtual display (closest to a
real screen); without it the browser runs headless. On a display,
`browser_viewport` resizes the real window, so phone and tablet checks reflow
the page at true breakpoints.

## Add it to your agent

**Claude Code**

```bash
claude mcp add ascended-browser -- uvx ascended-browser
# or: claude mcp add ascended-browser -- npx -y ascended-browser
```

**Codex**

```bash
codex mcp add ascended-browser -- uvx ascended-browser
```

Whether Codex asks before each browser action follows the permission mode you
pick in Codex: under **Full access** it just runs them; under **Ask for approval**
it asks first. In a sandboxed mode, `codex exec` cannot ask and fails every
call: use Full access, or pre-approve this server's tools by adding
`default_tools_approval_mode = "approve"` under `[mcp_servers.ascended-browser]`
in `~/.codex/config.toml`.

**opencode** (`opencode.json`)

```json
{
  "mcp": {
    "ascended-browser": { "type": "local", "command": ["uvx", "ascended-browser"], "enabled": true }
  }
}
```

**Cursor, Windsurf, Claude Desktop and other clients:** a stdio server with
command `uvx` and args `["ascended-browser"]`, or command `npx` and args
`["-y", "ascended-browser"]`.

## Tools

| Tool | What it does |
|---|---|
| `browser_open` | Open a URL (or several at once) and return what is on the page, each control with a ref |
| `browser_observe` | Look at the page again, or narrow to a region, a query or a filter |
| `browser_act` | `navigate`, `click`, `fill`, `fill_form`, `select`, `check`, `date`, `press`, `upload`, `scroll`, `wait`, `sequence`: each verified, each answered with what changed |
| `browser_extract` | Read the page's text and field state, query by CSS selector, pull repeated items into a JSON shape, or `read=console`, `read=network`, `read=inspect` to debug a page |
| `browser_screenshot` | A picture, when an observation cannot describe it: canvas, charts, visual layout; `compare_with` diffs against an earlier picture or another tab |
| `browser_viewport` | Resize to phone/tablet/desktop (Linux), emulate dark mode, reduced motion, forced colors or offline |
| `browser_evaluate` | Read-only JavaScript, policy-checked |
| `browser_tabs` | List, close or sleep tabs |
| `browser_flow` | Record a task once, replay it on the next page with new values |
| `wait_for_bot_wall` | Wait out a "checking your browser" page; press a Turnstile/reCAPTCHA checkbox if one blocks a form |

Long results come back clipped, with an `evidence_ref` that
`browser_extract` pages through, so a huge page cannot flood the agent's
context.

## Settings

| Variable | Default | |
|---|---|---|
| `ASCENDED_BROWSER_WINDOW` | hidden | `show` opens a visible window |
| `ASCENDED_DATA_DIR` | `~/.local/share/ascended/browser` | Browser profile (sign-ins persist), session files |
| `ASCENDED_RESULT_MAX_CHARS` | `24000` | Longer results are clipped with an `evidence_ref` |
| `ASCENDED_SETTING_<KEY>` | | Any browser setting, e.g. `ASCENDED_SETTING_BROWSER_WORKSPACE_OBSERVE_FORMAT=outline` |
| `ASCENDED_LOG_LEVEL` | `WARNING` | Logs go to stderr |

## Limits (0.1)

- **Resizing the window** (phone/tablet presets, multi-size screenshot
  grids) runs through Ascended's live view, which this package does not
  ship yet. Emulation (dark mode and the rest) works.
- **Saved logins** (`browser_login`) and **schema extraction backed by a
  model** need the Ascended app.
- One server process is one browser session: tabs and refs last until your
  client disconnects; the profile (cookies, sign-ins) lasts across sessions.

## How it is built

`src/ascended_browser/_app` is generated from Ascended by
`scripts/sync_from_ascended.py`: the browser modules copied as they are, the
browser tool dispatcher and result formatter extracted by reachability, and
every import of the rest of the app rewritten to `runtime/` (small standalone
stand-ins). The sync refuses any app import it cannot map.

Tested with Ascended's own stress harnesses run against this package
(`tests/stress/`), a client-side MCP smoke test (`tests/smoke_mcp.py`), and
live-website tasks given to real agents (`tests/agents/`).

What has been verified so far: Linux (Python 3.11, 3.12 and 3.14), with Claude
Code and Codex (0.160) on live-site tasks, opencode on a navigation task, and
the npm launcher through uvx and through its own venv. macOS and Windows should
work headless or with a visible window, but are untested, and window resizing
for phone/tablet checks is Linux-only for now.

## License

MIT. The bundled axe-core (`_app/browser_vendor/axe-core`) is MPL-2.0 and keeps
its notice in the file.
