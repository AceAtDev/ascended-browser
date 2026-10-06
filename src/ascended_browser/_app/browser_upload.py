"""Fail-closed file selection through observed inputs or captured choosers."""

from __future__ import annotations

import os
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ascended_browser._app.browser_deadline import browser_deadline


class UploadError(RuntimeError):
    """Upload failure with a stable machine-readable code and evidence."""

    def __init__(self, code: str, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = dict(result or {})
        self.result.setdefault("success", False)
        self.result.setdefault("verified", False)
        self.result.setdefault("error_code", code)
        self.result.setdefault("error", message)


def _expected_names(paths: Iterable[str]) -> list[str]:
    return [Path(path).name for path in paths]


def _identity_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key in ("tag", "type", "id", "name", "label"):
        wanted = " ".join(str(expected.get(key) or "").split()).casefold()
        observed = " ".join(str(actual.get(key) or "").split()).casefold()
        if wanted and wanted != observed:
            return False
    return True


async def set_input_files_verified(
    target: Any, *, selector: str, paths: list[str], target_ref: str,
    expected_identity: dict[str, Any] | None = None, timeout_ms: int = 10000,
) -> dict[str, Any]:
    async with browser_deadline(timeout_ms / 1000):
        return await _set_input_files_verified(
            target, selector=selector, paths=paths, target_ref=target_ref,
            expected_identity=expected_identity, timeout_ms=timeout_ms,
        )


async def _set_input_files_verified(
    target: Any,
    *,
    selector: str,
    paths: list[str],
    target_ref: str,
    expected_identity: dict[str, Any] | None = None,
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    """Set files on exactly one input and require exact filename evidence.

    ``target`` is a Playwright Page or Frame. Native file choosers are never used.
    """
    selector = str(selector or "").strip()
    target_ref = str(target_ref or "").strip()
    clean_paths = [os.path.abspath(os.path.expanduser(str(path))) for path in paths if str(path).strip()]
    expected = _expected_names(clean_paths)
    base = {
        "success": False,
        "verified": False,
        "target_ref": target_ref,
        "expected_filenames": expected,
        "observed_input_filenames": [],
        "visible_filename_matches": [],
        "verification_method": "none",
    }
    if not target_ref or not selector:
        raise UploadError("file_input_ref_required", "upload requires a fresh file-input ref", result=base)
    if not clean_paths:
        raise UploadError("upload_paths_required", "upload requires at least one file path", result=base)
    missing = [path for path in clean_paths if not os.path.isfile(path) or not os.access(path, os.R_OK)]
    if missing:
        raise UploadError("upload_file_unavailable", f"upload file is not readable: {Path(missing[0]).name}", result=base)
    # An empty file uploads "successfully" and reaches the site as a broken
    # attachment (a resume that was never saved). Refuse it before any effect.
    empty = [path for path in clean_paths if os.path.getsize(path) == 0]
    if empty:
        raise UploadError(
            "upload_file_empty",
            f"upload file is empty (0 bytes): {Path(empty[0]).name}; it may not have been saved",
            result=base,
        )

    locator = target.locator(selector)
    count = await locator.count()
    if count != 1:
        raise UploadError(
            "file_input_ref_ambiguous" if count > 1 else "stale_file_input_ref",
            f"file-input ref resolved to {count} elements",
            result=base,
        )
    locator = locator.first
    actual = await locator.evaluate(
        """el => ({
          tag: String(el.tagName || '').toLowerCase(),
          type: String(el.getAttribute('type') || '').toLowerCase(),
          id: String(el.id || ''),
          name: String(el.getAttribute('name') || ''),
          label: String(
            el.getAttribute('aria-label') ||
            (el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]')?.textContent : '') ||
            el.closest('label')?.textContent || ''
          ).replace(/\\s+/g, ' ').trim(),
          disabled: !!el.disabled,
          multiple: !!el.multiple
        })"""
    )
    if actual.get("tag") != "input" or actual.get("type") != "file":
        raise UploadError("target_not_file_input", "target ref is not an input[type=file]", result=base)
    if actual.get("disabled"):
        raise UploadError("file_input_disabled", "target file input is disabled", result=base)
    expected_frame_url = str((expected_identity or {}).get("frame_url") or "").strip()
    actual_frame_url = str(getattr(target, "url", "") or "").strip()
    if expected_frame_url and actual_frame_url and expected_frame_url != actual_frame_url:
        raise UploadError("stale_file_input_ref", "file-input frame changed since observation", result=base)
    if expected_identity and not _identity_matches(expected_identity, actual):
        raise UploadError("stale_file_input_ref", "file-input identity changed since observation", result=base)
    if len(clean_paths) > 1 and not actual.get("multiple"):
        raise UploadError("multiple_files_not_allowed", "target file input does not accept multiple files", result=base)

    try:
        before_text = await target.locator("body").inner_text(timeout=min(3000, timeout_ms))
    except Exception:
        before_text = ""
    # Hold the element itself: a page that replaces its input after reading
    # the file (Nokia) leaves the selector pointing at a fresh, empty input,
    # while the original still carries what it was given.
    handle = locator
    if hasattr(locator, "element_handle"):
        try:
            handle = await locator.element_handle(timeout=min(3000, timeout_ms))
        except Exception:
            handle = locator
    # The names the input held at its own change event. Sites that read the
    # files and then clear the input (value = '') left nothing to read back,
    # and three attachments that had landed were reported unverified
    # (2026-10-04: Workday, Jobvite, ChatGPT). Recorded on the input itself.
    try:
        await handle.evaluate("""el => {
          el.removeAttribute('data-odysseus-upload-witness');
          el.addEventListener('change', () => el.setAttribute('data-odysseus-upload-witness',
            JSON.stringify(Array.from(el.files || []).map(file => String(file.name || '')))),
            {once: true, capture: true});
        }""")
    except Exception:
        pass
    await locator.set_input_files(clean_paths, timeout=timeout_ms)

    replaced = not await target.locator(selector).count()
    observed: list[str] = []
    if not replaced:
        try:
            observed = [str(name) for name in (await locator.evaluate(
                "el => Array.from(el.files || []).map(file => String(file.name || '')).filter(Boolean)"
            ) or [])]
        except Exception:
            observed = []
    witnessed = await _witnessed_names(handle)
    base["observed_input_filenames"] = observed
    if witnessed is not None:
        base["witnessed_input_filenames"] = witnessed
    if replaced:
        base["input_replaced"] = True

    method = ("input_files" if Counter(observed) == Counter(expected)
              else "input_change_event" if witnessed is not None and Counter(witnessed) == Counter(expected)
              else "")
    if method == "input_files":
        # The input still holds the files: an ordinary form field, which the
        # page reads on submit. Nothing else to wait for.
        return {**base, "success": True, "verified": True, "verification_method": method,
                "file_selection_verified": True, "attachment_accepted": None, "submission_observed": False}

    # The page took the file out of its input (cleared or replaced it), so it
    # is handling the upload itself and shows the name when it is done:
    # Workday uploads to its server first. Watch for that, bounded, the way a
    # typed search is given time to answer.
    shown = await _wait_for_names_shown(target, expected, before_text, timeout_ms=timeout_ms)
    base["visible_filename_matches"] = shown
    all_shown = Counter(shown) == Counter(expected)
    if method == "input_change_event":
        if replaced and all_shown:
            # Keep the established name for a swapped input whose page shows
            # the file (Greenhouse); the change event only adds to it.
            method = "input_replaced_filename_shown"
        result = {**base, "success": True, "verified": True, "verification_method": method,
                  "file_selection_verified": True, "attachment_accepted": True if all_shown else None,
                  "submission_observed": False}
        if not all_shown:
            result["attachment_note"] = (
                f"the input took {', '.join(expected)}, but the page has not shown the filename yet; "
                "it may still be uploading. Check the page before uploading again."
            )
        return result
    if replaced and all_shown:
        # The exact input accepted the file and then its own document showed
        # the name (Greenhouse swaps the input for an attachment row).
        return {**base, "success": True, "verified": True,
                "verification_method": "input_replaced_filename_shown",
                "file_selection_verified": True, "attachment_accepted": True, "submission_observed": False}
    # No evidence that this input took the files: say exactly what was seen,
    # so the caller checks the page instead of uploading a second copy.
    seen = [
        "the input was replaced" if replaced else f"the input holds {observed or 'no files'}",
        "it reported no change" if witnessed is None else f"its change event reported {witnessed}",
        f"the page shows {shown}" if shown else "the page does not show the filename",
    ]
    raise UploadError(
        "upload_unverified",
        "Playwright set the files but exact filename evidence was not observed ("
        + "; ".join(seen) + ")",
        result=base,
    )


async def _witnessed_names(handle: Any) -> list[str] | None:
    """The names the input reported at its change event, or None if it never fired.

    Read through evaluate: it works on a Locator and on an ElementHandle,
    attached or not. ``ElementHandle.get_attribute`` takes no timeout, so the
    old read raised on every chooser upload and Workday's were all reported
    unverified (session 722b3c33).
    """
    try:
        raw = await handle.evaluate("el => el.getAttribute('data-odysseus-upload-witness')")
        names = json.loads(str(raw or "null"))
    except Exception:
        return None
    return [str(name) for name in names] if isinstance(names, list) else None


async def _wait_for_names_shown(
    target: Any, expected: list[str], before_text: str, *, timeout_ms: int,
) -> list[str]:
    """The expected names the page newly shows, once all appear or time runs out."""
    from ascended_browser._app.browser_deadline import remaining_seconds

    left = remaining_seconds(default=timeout_ms / 1000) or 0.0
    stop = asyncio.get_running_loop().time() + max(0.5, min(6.0, left - 1.0))
    shown: list[str] = []
    while True:
        try:
            text = await target.locator("body").inner_text(timeout=1000)
        except Exception:
            text = ""
        shown = [name for name in expected if name in text and name not in before_text]
        if Counter(shown) == Counter(expected) or asyncio.get_running_loop().time() >= stop:
            return shown
        await asyncio.sleep(0.25)


async def choose_files_verified(
    page: Any, root: Any, *, selector: str, paths: list[str], target_ref: str,
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    """Capture a page chooser before clicking its observed attachment trigger.

    No native-dialog keyboard automation or DOM value mutation is used. The
    chooser's exact input must belong to the trigger's frame and remain attached.
    """
    clean = [os.path.abspath(os.path.expanduser(str(path))) for path in paths if str(path).strip()]
    if not clean or any(not os.path.isfile(path) or not os.access(path, os.R_OK) for path in clean):
        raise UploadError("upload_file_unavailable", "Chooser files must exist and be readable before activation")
    async with browser_deadline(timeout_ms / 1000):
        trigger = root.locator(selector)
        if await trigger.count() != 1:
            raise UploadError("attachment_trigger_ambiguous", "Attachment trigger is missing or ambiguous")
        async with page.expect_file_chooser(timeout=timeout_ms) as pending:
            await trigger.first.click(timeout=timeout_ms)
        chooser = await pending.value
        element = chooser.element
        frame = await element.owner_frame()
        expected_frame = getattr(root, "main_frame", root)
        if chooser.page is not page or frame is not expected_frame:
            raise UploadError("file_chooser_target_mismatch", "Chooser belongs to a different page or frame")

        class ChosenInput:
            @property
            def first(self):
                return element

            async def count(self):
                return int(bool(await element.evaluate("el => el.isConnected")))

        class ChosenRoot:
            url = frame.url

            def locator(self, query):
                return ChosenInput() if query == "chosen-input" else frame.locator(query)

        result = await set_input_files_verified(
            ChosenRoot(), selector="chosen-input", paths=clean,
            target_ref=target_ref, timeout_ms=timeout_ms,
        )
        return {**result, "target_source": "captured_file_chooser"}
