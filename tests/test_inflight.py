"""The journal of actions that never reported back (see ascended_browser/inflight.py).

    .venv/bin/python -m pytest -q tests/test_inflight.py

The end-to-end check (interrupt, SIGTERM, SSH-style SIGHUP against a real
browser) is tests/turn_interrupt_mcp.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ascended_browser.inflight import Journal, interrupted_note, recovered_note

TAB = "tab_" + "a" * 32
PAGE = ('### browser_open\n**page:** Shop · https://shop.example/cart\n'
        '{"ref": "e1a2b3c4d5e", "role": "button", "text": "Place order"}')


def test_reads_are_not_journaled(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    for tool, args in (("browser_observe", {"tab_id": TAB}), ("browser_extract", {"tab_id": TAB}),
                       ("browser_act", {"tab_id": TAB, "action": {"kind": "wait"}}),
                       ("browser_act", {"tab_id": TAB, "action": {"kind": "scroll"}}),
                       ("browser_flow", {"action": "list"})):
        assert journal.begin(tool, args) is None, tool
    assert not list((tmp_path / "inflight").glob("*.json"))


def test_interrupted_action_is_told_once_with_its_element_and_page(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.learn(TAB, PAGE)
    key = journal.begin("browser_act", {"tab_id": TAB, "action": {"kind": "click", "ref": "e1a2b3c4d5e"}})
    journal.interrupted(key)
    journal.end(key)  # the handler's finally runs after the cancel
    assert journal.notes_for("tab_" + "b" * 32) == []
    told = journal.notes_for(TAB)
    note = interrupted_note(told)
    assert "click on 'Place order' on https://shop.example/cart" in note
    assert journal.notes_for(TAB) == []
    assert not list((tmp_path / "inflight").glob("*.json")), "told notes leave the journal"


def test_untold_interruption_stays_on_disk_for_the_next_server(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.learn(TAB, PAGE)
    journal.interrupted(journal.begin("browser_login", {"tab_id": TAB, "submit": True}))
    saved = json.loads(journal.file.read_text())
    assert saved[0]["what"] == "sign-in with the saved login (submitted)"


def test_a_dead_servers_unfinished_actions_are_recovered_once(tmp_path: Path) -> None:
    code = (
        "import os, sys; from pathlib import Path; from ascended_browser.inflight import Journal\n"
        "j = Journal(Path(sys.argv[1]))\n"
        f"j.learn({TAB!r}, {PAGE!r})\n"
        f"j.begin('browser_act', {{'tab_id': {TAB!r}, 'action': {{'kind': 'sequence', 'steps': ["
        "{'kind': 'fill', 'ref': 'e9'}, {'kind': 'wait'}, {'kind': 'click', 'ref': 'e1a2b3c4d5e'}]}})\n"
        "os._exit(0)  # dies mid-action: no cleanup\n"
    )
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True)
    found = Journal(tmp_path).recover()
    assert [e["what"] for e in found] == ["sequence (fill on e9, click on 'Place order')"]
    assert "https://shop.example/cart" in recovered_note(found)
    assert Journal(tmp_path).recover() == []


def test_a_live_servers_journal_is_left_alone(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.begin("browser_act", {"tab_id": TAB, "action": {"kind": "click", "ref": "e1"}})
    other = Journal.__new__(Journal)
    other.folder = journal.folder
    other.file = journal.folder / "1.json"  # pretend to be another process
    assert Journal.recover(other) == []      # this process is alive: its entries are not "unfinished"
    assert journal.file.exists()
