"""Saved logins: the vault CLI, and the scrubber that keeps its values from the agent.

    .venv/bin/python -m pytest -q tests/test_logins.py

Each test runs the CLI in a fresh data directory (the vault module opens its
file at import, so the CLI runs as a subprocess).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Anything this process imports opens a vault: never the developer's real one.
os.environ["ASCENDED_DATA_DIR"] = str(Path(__import__("tempfile").mkdtemp(prefix="ab-test-data-")))

PASSWORD = "Hunter2 Sekr!t&x<y>"
USERNAME = "Ada.Tester@example.org"


def run(data: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "ASCENDED_DATA_DIR": str(data)}
    return subprocess.run([sys.executable, "-m", "ascended_browser.cli", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


def py(data: Path, code: str) -> str:
    env = {**os.environ, "ASCENDED_DATA_DIR": str(data)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture
def data(tmp_path: Path) -> Path:
    added = run(tmp_path, "login", "add", "accounts.example.org", "--username", USERNAME,
                "--name", "Work", "--password-stdin", stdin=PASSWORD + "\n")
    assert added.returncode == 0, added.stderr
    return tmp_path


def test_add_list_never_prints_the_password(data: Path) -> None:
    listed = run(data, "login", "list")
    assert "Work" in listed.stdout and USERNAME in listed.stdout and "accounts.example.org" in listed.stdout
    assert PASSWORD not in listed.stdout + listed.stderr
    rows = json.loads(run(data, "login", "list", "--json").stdout)
    assert rows == [{"id": rows[0]["id"], "name": "Work", "username": USERNAME,
                     "sites": ["https://accounts.example.org"], "totp": False}]


def test_vault_file_is_owner_only(data: Path) -> None:
    mode = stat.S_IMODE((data / "logins.db").stat().st_mode)
    assert mode == 0o600


def test_edit_keeps_unchanged_fields_and_remove_deletes(data: Path) -> None:
    edited = run(data, "login", "edit", "Work", "--username", "ada@example.org")
    assert edited.returncode == 0, edited.stderr
    item = json.loads(py(data, "import json; from ascended_browser import logins; "
                               "print(json.dumps(logins.items(include_secrets=True)))"))[0]
    assert item["login"]["username"] == "ada@example.org"
    assert item["login"]["password"] == PASSWORD, "edit without --password must keep the saved one"
    assert run(data, "login", "edit", "accounts.example.org", "--password-stdin", stdin="new-pass-1\n").returncode == 0
    item = json.loads(py(data, "import json; from ascended_browser import logins; "
                               "print(json.dumps(logins.items(include_secrets=True)))"))[0]
    assert item["login"]["password"] == "new-pass-1"
    assert run(data, "login", "remove", item["id"][:6]).returncode == 0
    assert json.loads(run(data, "login", "list", "--json").stdout) == []


def test_unknown_login_is_a_clear_error(data: Path) -> None:
    missing = run(data, "login", "remove", "nope.example")
    assert missing.returncode == 1 and "No saved login matches" in missing.stderr


def test_scrub_covers_every_encoding_a_tool_can_return(data: Path) -> None:
    from urllib.parse import quote, quote_plus
    import html

    samples = [
        f"value: {PASSWORD}",
        f"POST body email={quote_plus(USERNAME)}&password={quote_plus(PASSWORD)}",
        f"url ?p={quote(PASSWORD, safe='')}",
        json.dumps({"password": PASSWORD, "who": USERNAME.lower()}),
        f"<input value=\"{html.escape(PASSWORD)}\">",
        f"Signed in as {USERNAME.upper()}",
    ]
    out = json.loads(py(data, "import json, sys; from ascended_browser.logins import scrub; "
                              f"print(json.dumps([scrub(s) for s in {samples!r}]))"))
    for text in out:
        assert "[redacted]" in text
        low = text.lower()
        for secret in (PASSWORD, USERNAME, quote_plus(PASSWORD), quote(PASSWORD, safe=""),
                       html.escape(PASSWORD), quote_plus(USERNAME)):
            assert secret.lower() not in low, (secret, text)


def test_scrub_sees_a_login_added_after_startup(tmp_path: Path) -> None:
    code = (
        "from ascended_browser import logins\n"
        "assert logins.scrub('pw=late-added-pass') == 'pw=late-added-pass'\n"
        "logins.save(sites_=['late.example'], username='late-user', password='late-added-pass')\n"
        "print(logins.scrub('pw=late-added-pass user late-user'))\n"
    )
    assert py(tmp_path, code).strip() == "pw=[redacted] user [redacted]"


def test_capture_mask_hook_point_exists() -> None:
    """install_capture_masks wraps browser_capture.sensitive_mask_locators; a re-sync must keep it."""
    from ascended_browser._app import browser_capture

    assert callable(browser_capture.sensitive_mask_locators)
    assert "sensitive_mask_locators(page)" in Path(browser_capture.__file__).read_text()


def test_login_state_stays_in_the_data_directory(tmp_path: Path) -> None:
    out = py(tmp_path, "from ascended_browser import logins; from ascended_browser._app import browser_login_broker as b; "
                       "logins.install(); print(b.RATE_LIMIT_FILE)")
    assert Path(out.strip()).is_relative_to(tmp_path)


def test_masks_grow_past_glyph_overhang() -> None:
    import base64
    import io

    from PIL import Image

    from ascended_browser.logins import MASK_GROW_PX, widen_masks

    image = Image.new("RGB", (40, 20), "white")
    image.paste((255, 0, 255), (10, 5, 20, 15))   # a mask
    image.putpixel((21, 10), (0, 0, 0))            # a glyph sliver just past its edge
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    out = Image.open(io.BytesIO(base64.b64decode(widen_masks(base64.b64encode(buffer.getvalue()).decode()))))
    assert out.getpixel((21, 10)) == (255, 0, 255)
    assert out.getpixel((20 + MASK_GROW_PX - 1, 10)) == (255, 0, 255)
    assert out.getpixel((30, 10)) == (255, 255, 255), "masks grow by a few pixels, not more"
    blank = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(blank, "PNG")
    untouched = base64.b64encode(blank.getvalue()).decode()
    assert widen_masks(untouched) == untouched, "a picture with no mask is passed through as it is"
