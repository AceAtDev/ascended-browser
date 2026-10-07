# Browser MCP QA quickstart for Codex CLI and Claude Code

Connect Ascended Browser to your coding agent, then ask it to inspect a local app and report bugs with evidence. Start with a disposable development environment, not production or real customer accounts.

## Install the browser MCP server

This guide pins version 0.1.5 so the commands refer to a specific release. Linux is the documented tested platform. The npm launcher needs Node/npm plus either uv, pipx, or Python 3.11+; it runs the same Python server as the uvx command.

Run these commands from your app's project directory:

```bash
npx -y ascended-browser@0.1.5 doctor
npx -y ascended-browser@0.1.5 fetch
```

`doctor` checks prerequisites. `fetch` downloads the pinned Camoufox browser, about 700 MB, so the first agent tool call doesn't wait for it. On Linux, Xvfb provides a virtual display; without it the browser runs headless. Install missing system dependencies through your normal machine setup process.

## Connect Codex CLI or Claude Code

Choose the client you use. Run the corresponding commands in your project directory, then start a new agent session.

**Codex CLI**

```bash
codex mcp add ascended-browser -- npx -y ascended-browser@0.1.5
codex mcp list
```

**Claude Code**

```bash
claude mcp add ascended-browser -- npx -y ascended-browser@0.1.5
claude mcp list
```

The list command checks configuration, not successful browser startup. Ask the agent to open the app and verify that it receives a page observation. Retain the client's approval controls; a browser can submit forms and change external state. For noninteractive Codex runs, see the [README's approval notes](../README.md#add-it-to-your-agent) rather than broadly disabling sandbox protections.

## Start your app and give the agent a bounded task

Use your app's existing dev-server command and its actual URL. The sample prompt assumes the app is available at `http://127.0.0.1:3000`; change it if your server uses another port. No particular framework is required.

```text
Use Ascended Browser to check http://127.0.0.1:3000.
First open the app and describe the main controls you can observe.
Walk through its main flow using disposable test data only.
You may submit local test forms, but don't make purchases, send messages,
delete records, or interact with external services. Stop if a step needs that.
Do not change the source code yet.

Inspect console errors and failed network requests during the flow.
Capture screenshots where they help explain a problem.
For each issue, report the steps, expected and actual behavior, relevant
console or network evidence, and any uncertainty. Distinguish app failures
from browser-tool errors. Report checks that passed and checks you couldn't do.

If an action is interrupted or its effect is uncertain, observe the current
page before deciding whether to repeat it. Don't blindly retry a submission.
```

Ask for a narrow flow when your app has several, for example a local signup fixture or changing a test setting. "Check everything" is not a defined test plan.

## Read console logs and failed requests

The agent can use `browser_extract` with `read=console` for logs and `read=network` with `failed_only=true` for failed requests. `browser_observe` supplies current controls and refs; `browser_act` interacts with those controls. Screenshots supplement those observations, rather than replacing the evidence.

Have the agent correlate errors with the step that triggered them. A third-party failure does not automatically establish an app defect, and an empty error log does not establish that the app works correctly.

## Report bugs with evidence, not guesses

Use this output structure. It is a template, not an observed bug report:

```text
Issue: [specific observed failure]
Environment: [URL, version/commit if known, viewport]
Steps: [actions taken, in order]
Expected: [requirement or explicitly stated assumption]
Actual: [observed page behavior]
Evidence: [request URL/status, console message, screenshot if available]
Reproduced: [number of attempts actually performed]
Uncertainty: [gaps and alternative explanations]
```

Don't add severity, root cause or reproduction counts without support. Once an issue is understood, authorize the agent to fix it and rerun the same flow. Keep deterministic tests for known regressions.

## Run a package smoke check without an agent model

To check the browser tools themselves, clone the repository and run its existing local-fixture smoke test. From the repository root, with Python 3.11+ and the release installed:

```bash
python -m venv .venv-qa
PIP_USER=0 .venv-qa/bin/python -m pip install ascended-browser==0.1.5
.venv-qa/bin/ascended-browser fetch
ASCENDED_BROWSER_BIN="$PWD/.venv-qa/bin/ascended-browser" .venv-qa/bin/python tests/smoke_mcp.py
```

The test opens a local form, fills an email field, reads it back, requests a screenshot and checks paging of clipped evidence. It prints `PASS` only when those assertions pass. It does not exercise your app, console/network bug diagnosis or a full Codex/Claude task. These shell paths are for Linux; they are not Windows instructions.

`PIP_USER=0` keeps installation inside the virtual environment even if your shell defaults to user-site installs.

### Observed smoke-test result

On October 6, 2026, the commands above were run on Linux with Python 3.14 and the published 0.1.5 package. The local-fixture test printed `PASS`:

| Check | Observed result |
|---|---|
| Open local form | Returned a tab and page observation |
| Fill email field | `probe@example.com` was read back by extraction |
| Request screenshot | Returned MCP image content |
| Read long page | Clipped evidence could be paged and contained paragraph text |

The server warned that its live-view service was unavailable; browser automation still completed. Codex's add/list commands were also checked in an isolated configuration directory. No full model-driven QA session or SSH recovery run was performed for this guide. Claude Code commands follow the project's existing documented setup and were not rerun in this environment.

## Use localhost correctly over SSH

For a direct answer to “Codex CLI has no browser when I SSH,” see the [browser-over-SSH setup guide](../codex-cli-browser-ssh/).

If you SSH into a Linux dev box and run the coding client there, install and configure the MCP there too. A stdio MCP runs on the client's machine, so `127.0.0.1` refers to that dev box, not your laptop. Your app must be reachable from that machine. No remote HTTP MCP endpoint is required for this arrangement.

Version 0.1.5 surfaces interrupted or unfinished actions in later tool results. It asks the agent to inspect before retrying; it does not enforce exactly-once actions or restore old refs after a server restart. A new session needs fresh observations. Treat the SSH arrangement as a setup pattern to validate on your own environment, not an end-to-end guarantee from this guide.

## Keep the limits and security boundary in view

- Linux resizing supports responsive checks, not a real mobile device or mobile-browser identity. macOS and Windows remain untested in the project's documentation.
- Saved login values are scrubbed from browser-tool results, but the vault is unencrypted and a coding agent with unrestricted shell/file access can read it. Use disposable accounts and review the [saved-login limitations](../README.md#saved-logins).
- Schema-shaped, model-driven extraction is not included in the standalone package.
- Exploratory agent checks are not a validated CI runner or a substitute for a regression suite.

For the complete tool list, settings and existing QA demo, see the [Ascended Browser README](../README.md). For Codex configuration details, see [OpenAI's MCP documentation](https://developers.openai.com/codex/mcp).
