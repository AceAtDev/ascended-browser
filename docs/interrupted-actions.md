# Interrupted browser actions: why a retry can become a second click

An agent losing a result does not mean the app lost the action. Ascended Browser 0.1.5 records unfinished state-changing actions and reports uncertainty to the next tool call or restarted session, so the agent can inspect before retrying.

## The failure case: the app accepted the request

A Reddit tester raised the risk of an SSH session ending while a browser action is mid-wait. Mohamed reported reproducing a duplicate-click problem and releasing 0.1.5. The technical question is broader: when a result disappears, did the click fail, finish or continue without the caller?

The repository's reproduction uses a disposable local checkout fixture. Clicking “Place order” sends a POST, waits six seconds and increments an in-memory order counter. There are no purchases, real accounts or external services involved.

If the client stops waiting after the POST reaches the app, that server-side counter can still increment. Repeating the click is a new action, not recovery of the first one.

## What was tested on October 6, 2026

The existing [interruption harness](https://github.com/AceAtDev/ascended-browser/blob/main/tests/turn_interrupt_mcp.py) was run on Linux with Python 3.14 and the published 0.1.5 package. It printed `PASS` for its four scenarios:

| Interruption | Observed behavior in this run |
|---|---|
| Client cancels and sends an MCP cancellation notification | Next observation returned promptly with a warning. The first order later completed. Deliberately clicking again created order #2. |
| Client stops waiting without sending cancellation | Next observation waited 5.9 seconds for the active action and saw order #1. No interruption warning was reported. Deliberately clicking again created order #2. |
| Server receives SIGTERM | New server opened successfully, saw one recorded order and reported the unfinished click once. No browser/Xvfb survivors were detected by the harness after its ten-second wait. |
| Process group receives SIGHUP | New server opened successfully, saw one recorded order and reported the unfinished click once. The harness found no surviving browser/Xvfb processes or leftover journal files. |

SIGHUP simulates an SSH-style hangup locally. This was **not an actual connection to a remote SSH host**, nor a full Codex or Claude agent evaluation. Timings describe this one run, not a performance guarantee.

The live-view service was unavailable in the test environment; browser automation continued. The killed-browser scenarios produced fixture-side broken-pipe messages when the app tried to answer a closed connection. The harness still passed its recovery assertions.

## Reproduce with the existing harness

Clone the repository, then run from its root with Python 3.11+ on Linux:

```bash
python -m venv .venv-qa
PIP_USER=0 .venv-qa/bin/python -m pip install ascended-browser==0.1.5
.venv-qa/bin/ascended-browser fetch
.venv-qa/bin/python tests/turn_interrupt_mcp.py "$PWD/.venv-qa/bin/ascended-browser"
```

The script starts its own localhost fixture, creates temporary browser data directories and sends cancellation, SIGTERM and SIGHUP only to its test processes. Its final `PASS` means the harness assertions passed; the deliberate re-clicks demonstrate why the warning is not exactly-once protection.

## What the warning changes

The journal can retain an action description and page context when a result never returns. A subsequent tool call or restarted session reports that the earlier action may already have taken effect.

The recovery pattern is:

1. Read the warning and identify the unfinished action.
2. Observe the current page with fresh refs. After a restart, reopen the appropriate page if needed.
3. Check the app's actual state, such as its recorded order count, rather than trusting a missing response.
4. Retry only when the evidence and authorization make it appropriate. If the effect remains uncertain, stop and report that uncertainty.

Client behavior matters: simply discarding a result does not necessarily send a cancellation notification. The second scenario shows why not every abandoned wait produces an interrupted-action warning.

## What this does not fix

- It does not undo an accepted request, restore a killed page's unsaved state, or preserve usable old element refs.
- It does not force an agent to heed the warning or prevent a second submission.
- It does not replace application-level idempotency keys, transaction checks or deterministic tests.
- These fixture tests do not establish a production CI integration or reliability across every SSH/client setup.

## Put it into a QA workflow

Include this instruction in the agent's task: “If an action is interrupted or its result is uncertain, inspect the current app state before retrying. Don't blindly repeat a submission.”

For configuration, a bounded QA prompt and console/network inspection, follow the [Codex CLI and Claude Code QA quickstart](../qa-quickstart/). For source and releases, see the [repository](https://github.com/AceAtDev/ascended-browser).
