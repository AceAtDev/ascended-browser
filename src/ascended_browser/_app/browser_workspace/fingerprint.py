"""A stable, per-profile Camoufox fingerprint.

Camoufox generates a fresh fingerprint on every launch. Measured across two
generations, 10 of its 21 config keys change — screen resolution, window size
and position, `navigator.hardwareConcurrency`. The profile keeps its cookies,
so every restart presents the sites you are signed into with the same session
arriving from what looks like a different computer. That is the shape of an
account takeover, and it is what re-auth prompts, device checks and captchas
are looking for.

So: generate once per profile, persist it, hand it back on every launch.

The pin is keyed on ``(browser_major, camoufox_library_version)`` and thrown
away when either moves:

* **browser major** is the only version Camoufox itself bakes into a
  fingerprint (``installed_verstr().split('.', 1)[0]``), and it selects the
  preset table (``PRESETS_V150_MIN_FF``). A pin claiming Firefox 135 while a
  Firefox 149 engine answers is *permanently* detectable — worse than
  re-randomising.
* **library version** decides the ``CAMOU_CONFIG`` schema. If a newer
  Camoufox reads keys an older pin does not carry, the binary falls back to
  the *real* values for those — a partial spoof, some properties faked and
  some genuine, which is worse than either extreme.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SIDECAR_NAME = ".odysseus-fingerprint.json"

#: Nested dataclasses inside a browserforge Fingerprint, so a persisted blob
#: can be rebuilt into the real object rather than left as plain dicts.
_NESTED = ("screen", "navigator", "videoCard")


def _versions() -> tuple[str, str]:
    """(browser major, camoufox library version) — both read without launching."""
    from camoufox.pkgman import _get_library_version, installed_verstr

    return installed_verstr().split(".", 1)[0], str(_get_library_version())


def _generate(os_name: str) -> dict[str, Any]:
    """Let Camoufox build the fingerprint, exactly as it would for itself.

    Never hand-assembled: a config we invented could pair a Windows user agent
    with Linux-only metrics, and pinning would make that contradiction
    permanent instead of momentary.
    """
    from dataclasses import asdict

    from camoufox.fingerprints import generate_fingerprint

    return asdict(generate_fingerprint(os=os_name))


def _rebuild(stored: dict[str, Any]) -> Any:
    """Turn a persisted blob back into a browserforge ``Fingerprint``.

    Passed as ``fingerprint=`` rather than as a raw ``config=`` on purpose:
    that is the path Camoufox sanctions, it runs ``check_custom_fingerprint()``
    so an incoherent pin is rejected at launch instead of shipping silently,
    and it avoids the LeakWarning that setting navigator/screen keys by hand
    otherwise raises on every single launch.
    """
    from browserforge.fingerprints import (
        Fingerprint, NavigatorFingerprint, ScreenFingerprint, VideoCard,
    )

    nested = {"screen": ScreenFingerprint, "navigator": NavigatorFingerprint,
              "videoCard": VideoCard}
    kwargs = {
        key: (nested[key](**value) if key in nested and isinstance(value, dict) else value)
        for key, value in stored.items()
    }
    return Fingerprint(**kwargs)


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="fingerprint-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_or_create(profile_dir: str | Path, *, os_name: str = "windows") -> dict[str, Any]:
    """The pinned fingerprint for this profile, regenerating on a version change.

    Returns a browserforge ``Fingerprint`` to pass as ``fingerprint=`` at
    launch, which Camoufox validates and then renders into its own config.
    """
    path = Path(profile_dir) / SIDECAR_NAME
    browser_major, library_version = _versions()

    stored: dict[str, Any] | None = None
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("[fingerprint] unreadable pin at %s; regenerating", path)
            stored = None

    if isinstance(stored, dict) and isinstance(stored.get("config"), dict):
        same_browser = str(stored.get("browser_major")) == browser_major
        same_library = str(stored.get("library_version")) == library_version
        if same_browser and same_library:
            try:
                return _rebuild(stored["config"])
            except Exception:
                log.warning("[fingerprint] pin at %s could not be rebuilt; regenerating", path)
        log.info(
            "[fingerprint] regenerating: browser %s->%s, camoufox %s->%s",
            stored.get("browser_major"), browser_major,
            stored.get("library_version"), library_version,
        )

    config = _generate(os_name)
    rebuilt = _rebuild(config)
    _write(path, {
        "browser_major": browser_major,
        "library_version": library_version,
        "os": os_name,
        "config": config,
    })
    log.info("[fingerprint] pinned a new fingerprint for %s", path.parent.name)
    return rebuilt
