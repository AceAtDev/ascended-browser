#!/usr/bin/env node
// npx entry point for ascended-browser. The server is a Python package; this
// runs it with the first launcher available: uvx (which can also fetch Python
// itself), pipx, or a private venv made with Python 3.11+.
//
// stdout is the MCP channel, so installers only ever write to stderr.
// ASCENDED_BROWSER_PACKAGE overrides what gets installed (a version spec or a
// wheel path); ASCENDED_BROWSER_LAUNCHER forces uvx, pipx or venv.
"use strict";
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const VERSION = require("../package.json").version;
const SPEC = process.env.ASCENDED_BROWSER_PACKAGE || `ascended-browser==${VERSION}`;
const ARGS = process.argv.slice(2);
const WINDOWS = process.platform === "win32";
const FORCED = (process.env.ASCENDED_BROWSER_LAUNCHER || "").trim();

function works(cmd, args) {
  const result = spawnSync(cmd, args, { stdio: ["ignore", "pipe", "ignore"], encoding: "utf8" });
  return result.status === 0 ? (result.stdout || "").trim() : null;
}

function run(cmd, args) {
  const child = spawn(cmd, args, { stdio: "inherit" });
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(signal, () => child.kill(signal));
  child.on("error", (err) => {
    process.stderr.write(`ascended-browser: could not start ${cmd}: ${err.message}\n`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => (signal ? process.kill(process.pid, signal) : process.exit(code ?? 0)));
}

function python() {
  const candidates = WINDOWS ? [["py", ["-3"]], ["python", []]] : [["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    const ok = works(cmd, [...pre, "-c", "import sys; print(int(sys.version_info >= (3, 11)))"]);
    if (ok === "1") return [cmd, pre];
  }
  return null;
}

function venvLaunch() {
  const found = python();
  if (!found) {
    process.stderr.write(
      "ascended-browser needs Python 3.11+ or uv. The easiest fix is uv, which brings its own Python:\n" +
        "  https://docs.astral.sh/uv/getting-started/installation/\n");
    process.exit(1);
  }
  const [cmd, pre] = found;
  const cache = process.env.XDG_CACHE_HOME || (WINDOWS ? process.env.LOCALAPPDATA : path.join(os.homedir(), ".cache"));
  const tag = SPEC === `ascended-browser==${VERSION}` ? VERSION : Buffer.from(SPEC).toString("hex").slice(-16);
  const venv = path.join(cache || os.tmpdir(), "ascended-browser", `venv-${tag}`);
  const bin = WINDOWS ? path.join(venv, "Scripts", "ascended-browser.exe") : path.join(venv, "bin", "ascended-browser");
  if (!fs.existsSync(bin)) {
    const quiet = { stdio: ["ignore", process.stderr, process.stderr] };
    process.stderr.write(`ascended-browser: installing ${SPEC} into ${venv} (first run only)\n`);
    if (spawnSync(cmd, [...pre, "-m", "venv", venv], quiet).status !== 0) process.exit(1);
    const pip = WINDOWS ? path.join(venv, "Scripts", "python.exe") : path.join(venv, "bin", "python");
    if (spawnSync(pip, ["-m", "pip", "install", "--quiet", SPEC], quiet).status !== 0) {
      fs.rmSync(venv, { recursive: true, force: true });
      process.exit(1);
    }
  }
  run(bin, ARGS);
}

const launchers = {
  uvx: () => run("uvx", ["--from", SPEC, "ascended-browser", ...ARGS]),
  pipx: () => run("pipx", ["run", "--spec", SPEC, "ascended-browser", ...ARGS]),
  venv: venvLaunch,
};

if (FORCED) {
  if (!launchers[FORCED]) {
    process.stderr.write(`ascended-browser: ASCENDED_BROWSER_LAUNCHER must be uvx, pipx or venv\n`);
    process.exit(2);
  }
  launchers[FORCED]();
} else if (works("uvx", ["--version"]) !== null) {
  launchers.uvx();
} else if (works("pipx", ["--version"]) !== null) {
  launchers.pipx();
} else {
  launchers.venv();
}
