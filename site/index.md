# Give your coding agent a browser.<br>Then check what it shipped.

Ascended Browser is an open-source, local browser MCP for **Codex CLI, Claude Code and other MCP clients**. Walk through your development app, inspect console logs and network requests, and collect evidence before changing code.

<div class="actions"><a class="primary" href="qa-quickstart/">Run your first QA check <span aria-hidden="true">↗</span></a><a class="secondary" href="https://github.com/AceAtDev/ascended-browser">View on GitHub</a></div>

<div class="chips"><span>Local stdio MCP</span><span>Linux tested</span><span>Open source · MIT</span><span>Release 0.1.5</span></div>

## From a click to a useful report

<div class="cards">
<section><span class="step">01 / INTERACT</span><h3>Walk through the app</h3><p>Open pages, inspect controls and perform a bounded flow using observed element refs.</p></section>
<section><span class="step">02 / INSPECT</span><h3>Read what went wrong</h3><p>Inspect console errors and failed network requests. Screenshots give the report visual context.</p></section>
<section><span class="step">03 / RECHECK</span><h3>Inspect before retrying</h3><p>Interrupted-action warnings surface uncertainty. They do not guarantee exactly-once clicks.</p></section>
</div>

## See the development workflow

<figure><img src="https://raw.githubusercontent.com/AceAtDev/ascended-browser/main/videos/dev-react.gif" alt="Ascended Browser development and QA demo" width="1280" height="720" loading="lazy"><figcaption>Existing development demo. The <a href="https://github.com/AceAtDev/ascended-browser#see-it-work">repository explains its edits and annotations</a>. This is not an SSH interruption-recovery demonstration.</figcaption></figure>

## One connection, then a bounded task

**Running Codex CLI over SSH and missing a browser?** Run a browser MCP on the Linux machine where Codex runs. [Follow the Codex CLI browser-over-SSH guide](codex-cli-browser-ssh/) for commands, localhost behavior and alternatives. Ascended is one option, not the only browser MCP for Codex.

From your project directory, after checking prerequisites:

```bash
npx -y ascended-browser@0.1.5 doctor
npx -y ascended-browser@0.1.5 fetch
codex mcp add ascended-browser -- npx -y ascended-browser@0.1.5
```

The launcher needs Node/npm plus uv, pipx or Python 3.11+. The initial Camoufox download is about 700 MB. [Follow the full setup guide](qa-quickstart/) for approval controls, a QA prompt and tested smoke-check commands.

## Pick the workflow, not the hype

Playwright MCP and Chrome DevTools MCP are valid alternatives; console and network inspection are not exclusive here. Ascended offers a local Camoufox-backed MCP with verified actions, bounded evidence and interrupted-action warnings. Choose it when that workflow fits your coding harness.

This is **agent-assisted exploratory QA**, not a validated CI integration or a replacement for deterministic regression tests. Linux is the documented tested platform. Mobile-sized viewports are responsive checks, not real mobile-device emulation. The saved-login vault is unencrypted, so use disposable accounts and read the [security boundaries](qa-quickstart/#keep-the-limits-and-security-boundary-in-view).

## Start with a task you can reproduce

- [Set up a browser MCP for Codex CLI or Claude Code](qa-quickstart/): installation, prompt, console/network evidence and a report template.
- [Give Codex CLI a browser on an SSH dev box](codex-cli-browser-ssh/): direct answer, setup commands, alternatives and recovery limits.
- [Understand interrupted browser actions](interrupted-actions/): the failure scenario, reproduction harness and limits of recovery warnings.
- [Report an issue](https://github.com/AceAtDev/ascended-browser/issues): include your client, package version, environment and a disposable reproduction.
