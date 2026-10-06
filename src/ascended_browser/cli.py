"""``ascended-browser``: run the MCP server, or check / prepare this machine.

    ascended-browser            # MCP server on stdio (what a client launches)
    ascended-browser doctor     # what is installed, what is missing
    ascended-browser fetch      # download the Camoufox browser now (otherwise on first use)
    ascended-browser login ...  # saved logins browser_login can fill: add, list, edit, remove
    ascended-browser signin URL # sign in by hand in a visible window; the profile keeps it
"""
from __future__ import annotations

import argparse
import getpass
import json
import platform
import shutil
import sys

from . import __version__
from .browser_build import PINNED


def _camoufox_path() -> str:
    """The pinned Camoufox build, if it is installed."""
    try:
        from .browser_build import installed_path

        path = installed_path()
        return str(path) if path else ""
    except Exception:
        return ""


def doctor() -> int:
    from .runtime.paths import data_dir
    from .server import _window_mode

    checks = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "data_dir": str(data_dir()),
        "camoufox_browser": _camoufox_path() or f"missing (Camoufox {PINNED}): run `ascended-browser fetch`",
        "window": {False: "visible", True: "headless", "virtual": "virtual display (Xvfb)"}[_window_mode()],
        "xvfb": shutil.which("Xvfb") or ("not needed" if not sys.platform.startswith("linux") else "missing (headless instead)"),
    }
    print(json.dumps(checks, indent=2))
    return 0 if checks["camoufox_browser"] and not checks["camoufox_browser"].startswith("missing") else 1


def fetch() -> int:
    from .browser_build import pin

    try:
        path = pin(download=True)
    except Exception as exc:
        print(f"Camoufox {PINNED} fetch failed: {exc}", file=sys.stderr)
        return 1
    print(path, file=sys.stderr)
    return 0


def _secret(prompt: str, *, from_stdin: bool) -> str:
    """A secret from a hidden prompt, or the first line of stdin; never from argv."""
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass(prompt)


def _row(item: dict) -> str:
    login = item.get("login") or {}
    flags = "+totp" if login.get("totp") else ""
    from .logins import sites

    return f"{item['id'][:8]}  {item['name']:<24} {login.get('username') or '-':<28} {' '.join(sites(item))} {flags}".rstrip()


def login(options: argparse.Namespace) -> int:
    from . import logins

    try:
        if options.action == "list":
            found = logins.items(include_secrets=True)
            if options.json:
                print(json.dumps([{"id": i["id"], "name": i["name"], "username": (i["login"] or {}).get("username", ""),
                                   "sites": logins.sites(i), "totp": bool((i["login"] or {}).get("totp"))}
                                  for i in found], indent=2))
            elif not found:
                print("No saved logins. Add one: ascended-browser login add example.com --username you@example.com")
            else:
                print("\n".join(_row(item) for item in found))
            return 0
        if options.action == "remove":
            item = logins.resolve(options.login)
            logins.remove(item["id"])
            print(f"Removed {item['name']!r}.")
            return 0
        if options.action == "add":
            username = options.username if options.username is not None else input("Username or email: ").strip()
            password = _secret("Password: ", from_stdin=options.password_stdin)
            if not password:
                print("No password given; nothing saved.", file=sys.stderr)
                return 1
            totp = _secret("TOTP secret or otpauth:// URI: ", from_stdin=False) if options.totp else ""
            saved = logins.save(sites_=options.sites, username=username, password=password, totp=totp,
                                name=options.name or "")
            print(f"Saved {saved['name']!r} ({saved['id'][:8]}) for {' '.join(options.sites)}.")
            return 0
        # edit
        item = logins.resolve(options.login)
        password = (_secret("New password: ", from_stdin=options.password_stdin)
                    if options.password or options.password_stdin else None)
        totp = "" if options.no_totp else (_secret("TOTP secret or otpauth:// URI: ", from_stdin=False)
                                           if options.totp else None)
        saved = logins.save(sites_=options.sites or [], username=options.username, password=password,
                            totp=totp, name=options.name or "", existing=item)
        print(f"Updated {saved['name']!r} ({saved['id'][:8]}).")
        return 0
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing saved.", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ascended-browser", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--version", action="version", version=f"ascended-browser {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.add_parser("serve", help="the MCP server on stdio (the default)")
    commands.add_parser("doctor", help="what is installed, what is missing")
    commands.add_parser("fetch", help="download the browser now")
    signin = commands.add_parser("signin", help="sign in to a site by hand; the browser profile keeps it")
    signin.add_argument("url")

    login_cmd = commands.add_parser("login", help="saved logins the agent can use with browser_login")
    actions = login_cmd.add_subparsers(dest="action", metavar="action", required=True)
    add = actions.add_parser("add", help="save a login (the password is prompted, never an argument)")
    add.add_argument("sites", nargs="+", metavar="site",
                     help="the sign-in page's site, e.g. github.com (several for one account on several hosts)")
    add.add_argument("--username", help="prompted when omitted")
    add.add_argument("--name", help="what the agent calls this account (default: the site)")
    add.add_argument("--totp", action="store_true", help="also save a TOTP secret (prompted)")
    add.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    actions.add_parser("list", help="saved logins (no passwords)").add_argument("--json", action="store_true")
    edit = actions.add_parser("edit", help="change a saved login; only the fields you pass change")
    edit.add_argument("login", help="id (or prefix), name or site")
    edit.add_argument("--site", dest="sites", action="append", metavar="SITE", help="replace the sites (repeatable)")
    edit.add_argument("--username")
    edit.add_argument("--name")
    edit.add_argument("--password", action="store_true", help="prompt for a new password")
    edit.add_argument("--password-stdin", action="store_true", help="read the new password from stdin")
    edit.add_argument("--totp", action="store_true", help="prompt for a new TOTP secret")
    edit.add_argument("--no-totp", action="store_true", help="remove the TOTP secret")
    remove = actions.add_parser("remove", help="delete a saved login")
    remove.add_argument("login", help="id (or prefix), name or site")
    return parser


def main() -> None:
    options = _parser().parse_args()
    if options.command == "doctor":
        sys.exit(doctor())
    if options.command == "fetch":
        sys.exit(fetch())
    if options.command == "login":
        sys.exit(login(options))
    if options.command == "signin":
        from .signin import signin

        sys.exit(signin(options.url))
    from .server import run

    run()


if __name__ == "__main__":
    main()
