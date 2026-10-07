# How do I give Codex CLI a browser when I SSH into a Linux machine?

**Run a browser MCP server on the Linux machine where Codex CLI runs, then connect it with `codex mcp add`.** OpenAI's [browser documentation](https://developers.openai.com/codex/app/browser) says its built-in browser is not available in Codex CLI. The CLI can instead use [MCP servers](https://developers.openai.com/codex/mcp), including Ascended Browser, Playwright MCP and Chrome DevTools MCP.

Ascended Browser is one open-source option for local, agent-assisted QA. It supplies a Camoufox-backed browser, interactions using observed controls, screenshots, read-only console/network inspection and interrupted-action warnings. It is not an SSH client and does not add a built-in browser to Codex itself.

## Connect the browser on the remote dev box

After SSHing into your Linux dev box, run these commands **on that box**, from the project directory:

```bash
npx -y ascended-browser@0.1.5 doctor
npx -y ascended-browser@0.1.5 fetch
codex mcp add ascended-browser -- npx -y ascended-browser@0.1.5
codex mcp list
```

Use a new Codex session after configuring it. The launcher requires Node/npm and uv, pipx or Python 3.11+. The initial browser download is about 700 MB. `doctor` reports missing prerequisites; Linux can use Xvfb for a virtual display, or run headless without it. See the [full QA quickstart](../qa-quickstart/) for installation details and approval notes.

The add/list commands and package smoke test were checked locally on Linux. This page describes a remote setup pattern; it is not a report of a full Codex-over-SSH test.

## Which machine does localhost mean?

In this setup, Codex CLI and the stdio MCP server run on the **remote Linux box**. A browser URL such as `http://127.0.0.1:3000` therefore addresses that box, not your laptop. Start your app there using its normal dev-server command, and give the agent the actual address and port.

If Codex runs on your laptop instead, this configuration starts the MCP on your laptop. That is a different arrangement; don't assume it will reach a remote app without an appropriate network or forwarding setup. The commands above do not expose a remote HTTP MCP endpoint.

## What should I ask Codex to do?

Use a disposable local app and a narrow test flow. For example:

```text
Use Ascended Browser to open http://127.0.0.1:3000.
Inspect the page, then walk through the main flow using disposable test data.
Check console errors and failed network requests.
Report observed failures with reproduction steps and evidence.
Don't edit code or perform purchases, deletions, messaging or external actions.
If a click is interrupted or its effect is uncertain, inspect current state
before deciding whether to retry. Don't blindly repeat a submission.
```

Change the address to match your app. The [QA quickstart](../qa-quickstart/) contains a fuller task prompt and bug-report template.

## Which browser MCP should I choose?

| Option | What to investigate for your setup |
|---|---|
| [Ascended Browser](https://github.com/AceAtDev/ascended-browser) | Local Camoufox workflow, observed-control actions, bounded evidence and interruption warnings. Linux is the documented tested platform. |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Official configuration, browser availability and its accessibility-snapshot interaction workflow. |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Official Chrome configuration and browser debugging tools, including console and network inspection. |

These are alternatives, not a performance ranking. Console/network inspection is not unique to Ascended. Headless browsers can test real apps and generate real network traffic; the choice is about tools, configuration and interaction workflow, not whether the testing is “real.”

## What happens if the SSH session dies during a click?

The app may already have accepted the request. Ascended 0.1.5 reports unfinished actions to help the next session inspect before retrying, but does not guarantee exactly-once execution. The [interruption case study](../interrupted-actions/) reports actual local tests, including an SSH-style SIGHUP simulation and deliberate retries that created a second order. It does not claim a real remote SSH recovery run.

## Does this make a CI test runner?

No. It can support exploratory QA by an agent, but the current guide does not establish a validated unattended CI integration. Keep deterministic regression tests and application-level duplicate protection. Saved-login values are scrubbed from browser-tool outputs, but the vault is unencrypted and unrestricted shell access can read it; use disposable test accounts.

## Sources and scope

Reviewed October 6, 2026. Product instructions are pinned to release 0.1.5; OpenAI's availability and configuration documentation can change.

- [OpenAI browser availability](https://developers.openai.com/codex/app/browser)
- [OpenAI Codex MCP configuration](https://developers.openai.com/codex/mcp)
- [Ascended Browser source and security limitations](https://github.com/AceAtDev/ascended-browser)
- [QA package smoke-check results](../qa-quickstart/#observed-smoke-test-result)
