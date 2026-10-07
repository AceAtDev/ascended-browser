<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/AceAtDev/ascended-browser/raw/main/docs/assets/ascended-browser-dark.png">
    <img src="https://github.com/AceAtDev/ascended-browser/raw/main/docs/assets/ascended-browser-light.png" alt="ascended-browser icon" width="96" height="96">
  </picture>
</p>
<h1 align="center">Ascended Browser: browser MCP for coding-agent QA</h1>

<p align="center"><strong>A real browser for your AI agent. One command to add.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/ascended-browser/"><img src="https://img.shields.io/pypi/v/ascended-browser" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/ascended-browser"><img src="https://img.shields.io/npm/v/ascended-browser" alt="npm"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
</p>

A local, open-source browser MCP server for Codex CLI, Claude Code and other
MCP clients. Use it for interactive browser automation and front-end QA:
inspect console logs and network requests, check responsive layouts, and
collect screenshots alongside what happened on the page.

Your agent opens pages, reads
them, and acts on them through **verified actions**: it fills a whole form in
one call, picks "Mrs." from a custom React dropdown by name, and is told what
the page did in response, instead of clicking coordinates and hoping.

It is the browser from Ascended, packaged on its own:
[Camoufox](https://camoufox.com) (a hardened Firefox that looks like a
person's browser to the sites it visits) behind the same tool dispatcher, page
reading and result formatting Ascended's own agent uses.

## Start with a QA check

[**Read the documentation site**](https://aceatdev.github.io/ascended-browser/):
[QA setup](https://aceatdev.github.io/ascended-browser/qa-quickstart/) and
[interrupted-action reproduction](https://aceatdev.github.io/ascended-browser/interrupted-actions/).

Running Codex CLI over SSH and missing a browser? [Connect a browser MCP on your Linux dev box](https://aceatdev.github.io/ascended-browser/codex-cli-browser-ssh/) with setup commands, localhost guidance and alternatives.

[**Set up a browser MCP for Codex CLI or Claude Code and check your app**](docs/qa-quickstart.md).
The guide covers installation, a copy-paste QA prompt, console and network
inspection, bug-report structure, and what to do after an interrupted action.

- **Check a local app:** walk through a flow and report errors before changing code.
- **Inspect the evidence:** read failed requests and console errors, then capture the broken state.
- **Use a Linux dev box:** run the client, MCP server and app on the same machine;
  the guide explains how localhost behaves when you connect over SSH.

This is agent-assisted exploratory QA, not a replacement for deterministic
regression tests or a validated CI integration. Browser MCP tools are also
available from projects such as Playwright MCP and Chrome DevTools MCP;
console and network inspection are not exclusive to this server.

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

<a href="https://github.com/AceAtDev/ascended-browser/blob/main/videos/login.mp4"><img src="https://github.com/AceAtDev/ascended-browser/raw/main/videos/login.gif" alt="Saved logins. A login is saved once with the CLI; the agent signs in with browser_login; your screen shows the account while every result and screenshot the agent gets has it redacted." width="760"></a>

**Saved logins.** You save a login once; the agent signs in with it and never sees it. Built from a real run (`demo/login_demo.py`, then `demo/login_explainer.py`): the CLI's output, the screen, and the exact text and pictures the agent got back.

Click any clip for the full-quality MP4.

## Install

From Python (3.11 or newer) or from npm; both run the same server.

```bash
uvx ascended-browser doctor          # checks the machine; downloads nothing
uvx ascended-browser fetch           # downloads the browser now (~700 MB; otherwise on first use)

npx -y ascended-browser doctor       # the same, from npm
```

The browser is Camoufox 135.0.1-beta.24, the build every test here ran on; it is
pinned, so a newer Camoufox release never changes what your agent drives, and
any other Camoufox you have installed is left as it is. Run `fetch` once before
adding the server to an agent, so its first tool call does not wait for the
download.

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
| `browser_extract` | Read the page's text and field state, `find` a phrase, list every match of a CSS selector with its text and attributes (every product link, every price), or `read=console`, `read=network`, `read=inspect`, `read=audit`, `read=design` to debug a page |
| `browser_screenshot` | A picture, when an observation cannot describe it: canvas, charts, visual layout; `compare_with` diffs against an earlier picture or another tab |
| `browser_viewport` | Resize to phone/tablet/desktop (Linux), emulate dark mode, reduced motion, forced colors or offline |
| `browser_evaluate` | Read-only JavaScript, policy-checked |
| `browser_tabs` | List, close or sleep tabs |
| `browser_flow` | Record a task once, replay it on the next page with new values |
| `browser_login` | Sign in with a login you saved (see [Saved logins](#saved-logins)); the agent never sees its values |
| `wait_for_bot_wall` | Wait out a "checking your browser" page; press a Turnstile/reCAPTCHA checkbox if one blocks a form |

Long results come back clipped, with an `evidence_ref` that
`browser_extract` pages through, so a huge page cannot flood the agent's
context.

Every action waits for the page to settle, under a hard timeout, and says
when its effect could not be confirmed. If a turn ends while one is still
running, the next turn is told instead of finding a page it cannot explain:
an interrupted click (you stopped the turn) is named in the next result for
that tab, and an action whose server died (`claude -p` ending, an SSH session
dropping) is listed with the first result of the next session, so the agent
checks before it repeats an order or a form.

## Saved logins

Save a login once and the agent can sign in with it, without ever seeing it.

```bash
uvx ascended-browser login add github.com --username you@example.com   # prompts for the password
uvx ascended-browser login add accounts.example.com --name Work --totp  # also a TOTP secret (or otpauth:// URI)
uvx ascended-browser login list                                         # names, usernames, sites; never passwords
uvx ascended-browser login edit Work --password                         # change only what you pass
uvx ascended-browser login remove Work
```

The site is the host of the **sign-in page** (`accounts.example.com`, not
`example.com`, when they differ); pass several for one account on several
hosts. Passwords and TOTP secrets come from a hidden prompt, or from stdin
with `--password-stdin`, never from the command line, where they would end up
in shell history and the process list.

Then ask your agent to sign in. `browser_open` tells it a saved login exists
for the page, and `browser_login` finds the username, password and
one-time-code fields and types the values in (`submit: true` also presses
the button). What the agent gets:

- **Every tool result is scrubbed** of every saved username, password and TOTP
  secret, also URL-encoded, JSON- or HTML-escaped: `browser_observe`,
  `browser_evaluate` (reading `input.value` returns `[redacted]`),
  `browser_extract` (page text, field values, network bodies) and action
  results. A page that prints "Signed in as you@example.com" reads as
  "Signed in as [redacted]".
- **Every screenshot is masked** before it reaches the agent: password, card
  and one-time-code fields, username and email fields, and any saved value
  shown as page text.

For sites a saved login cannot fill (single sign-on, passkeys, a CAPTCHA, a
code sent by email), sign in by hand once:

```bash
uvx ascended-browser signin https://example.com/login   # opens the browser; sign in, then press Enter
```

The cookies stay in the browser profile that every later agent session
starts from. Close running agent sessions first, so the sign-in lands in the
saved profile rather than a session's copy.

The vault is `logins.db` in the data directory, readable only by your user,
and not encrypted (like `gh` or `aws` credentials files). The scrubbing and
masking cover what the **browser tools** return. An agent that also has a
shell or file tools (Claude Code, Codex) runs as you and can read any file you
can, this one included. Deny its file tools the path (in Claude Code,
`"deny": ["Read(~/.local/share/ascended/**)"]` under `permissions` in
`~/.claude/settings.json`) and keep shell commands on approval; an agent free
to run any command can still reach the file. Values shorter than 4 characters
are not scrubbed, and a value the page changes (the last four digits of a
card, say) is not matched.

## Settings

| Variable | Default | |
|---|---|---|
| `ASCENDED_BROWSER_WINDOW` | hidden | `show` opens a visible window |
| `ASCENDED_DATA_DIR` | `~/.local/share/ascended/browser` | Browser profile (sign-ins persist), saved logins (`logins.db`), session files |
| `ASCENDED_RESULT_MAX_CHARS` | `24000` | Longer results are clipped with an `evidence_ref` |
| `ASCENDED_SETTING_<KEY>` | | Any browser setting, e.g. `ASCENDED_SETTING_BROWSER_WORKSPACE_OBSERVE_FORMAT=outline` |
| `ASCENDED_LOG_LEVEL` | `WARNING` | Logs go to stderr |

## Limits

- **Window resizing** (`browser_viewport` phone/tablet/desktop presets or any
  width and height) works on Linux: on the package's own virtual display by
  default, or on your X11 display with `ASCENDED_BROWSER_WINDOW=show`. On macOS
  and Windows the window keeps its launch size; emulation (dark mode, reduced
  motion, forced colors, offline) works everywhere. Screenshot grids across
  several sizes in one call are not included.
- **Schema-shaped extraction** (a model reads the page into your JSON shape)
  is an Ascended app feature that needs a model, so it is not in this package
  and its parameters are not exposed. Everything listed under Tools runs
  without a model.
- One server process is one browser session: tabs and refs last until your
  client disconnects; the profile (cookies, sign-ins) lasts across sessions.
  Several sessions can run at once: the first one uses the saved profile, and
  any other one started while it runs gets its own copy, already signed in to
  whatever the saved profile was. Sign-ins made in a copy end with that
  session.

## How it is built

`src/ascended_browser/_app` is generated from Ascended by
`scripts/sync_from_ascended.py`: the browser modules copied as they are, the
browser tool dispatcher and result formatter extracted by reachability, and
every import of the rest of the app rewritten to `runtime/` (small standalone
stand-ins). The sync refuses any app import it cannot map.

Tested with Ascended's own stress harnesses run against this package
(`tests/stress/`), a client-side MCP smoke test (`tests/smoke_mcp.py`), and
live-website tasks given to real agents (`tests/agents/`). Saved logins have
their own: `tests/test_logins.py` (the vault CLI and the scrubber),
`tests/login_redaction_mcp.py` (a real client signs in on a page that echoes
the login into its text, DOM and network, and no tool may show it; with
tesseract installed it also reads the screenshots) and
`tests/signin_persists.py`.

What has been verified so far: Linux (Python 3.11, 3.12 and 3.14), with Claude
Code and Codex (0.160) on live-site tasks, opencode on a navigation task, and
the npm launcher through uvx and through its own venv. macOS and Windows should
work headless or with a visible window, but are untested.

## License

MIT. The bundled axe-core (`_app/browser_vendor/axe-core`) is MPL-2.0 and keeps
its notice in the file.
