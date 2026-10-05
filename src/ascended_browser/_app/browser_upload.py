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
    # The names the input held at its own change event. Sites that read the
    # files and then clear the input (value = '') left nothing to read back,
    # and three attachments that had landed were reported unverified
    # (2026-10-04: Workday, Jobvite, ChatGPT). Recorded on the input itself.
    try:
        await locator.evaluate("""el => {
          el.removeAttribute('data-odysseus-upload-witness');
          el.addEventListener('change', () => el.setAttribute('data-odysseus-upload-witness',
            JSON.stringify(Array.from(el.files || []).map(file => String(file.name || '')))),
            {once: true, capture: true});
        }""")
    except Exception:
        pass
    await locator.set_input_files(clean_paths, timeout=timeout_ms)
    if not await target.locator(selector).count():
        # The widget took the file and replaced its input with an attachment
        # row (Greenhouse). Reading the gone input waited out the whole budget
        # and reported an uncertain effect. Here the exact input accepted the
        # file and then its own document showed the name, so that is the
        # evidence — not a name appearing anywhere while the input remains.
        for _ in range(10):
            try:
                after_text = await target.locator("body").inner_text(timeout=1000)
            except Exception:
                after_text = ""
            shown = [name for name in expected if name in after_text and name not in before_text]
            if Counter(shown) == Counter(expected):
                return {**base, "success": True, "verified": True,
                        "verification_method": "input_replaced_filename_shown",
                        "visible_filename_matches": shown, "file_selection_verified": True,
                        "attachment_accepted": True, "submission_observed": False}
            await asyncio.sleep(0.3)
        raise UploadError(
            "upload_unverified",
            "the file input was replaced after the files were set, and its page did not show the filename",
            result=base,
        )
    observed = await locator.evaluate("el => Array.from(el.files || []).map(file => String(file.name || '')).filter(Boolean)")
    observed = [str(name) for name in (observed or [])]
    base["observed_input_filenames"] = observed
    if Counter(observed) == Counter(expected):
        return {**base, "success": True, "verified": True, "verification_method": "input_files",
                "file_selection_verified": True, "attachment_accepted": None, "submission_observed": False}
    try:
        witnessed = json.loads(str(await locator.get_attribute("data-odysseus-upload-witness", timeout=1000) or "null"))
    except Exception:
        witnessed = None
    if isinstance(witnessed, list) and Counter(str(name) for name in witnessed) == Counter(expected):
        base["witnessed_input_filenames"] = witnessed
        return {**base, "success": True, "verified": True, "verification_method": "input_change_event",
                "file_selection_verified": True, "attachment_accepted": None, "submission_observed": False}

    try:
        await target.page.wait_for_timeout(500) if hasattr(target, "page") else await target.wait_for_timeout(500)
    except Exception:
        pass
    try:
        after_text = await target.locator("body").inner_text(timeout=min(3000, timeout_ms))
    except Exception:
        after_text = ""
    visible_matches = [name for name in expected if name in after_text and name not in before_text]
    base["visible_filename_matches"] = visible_matches
    # A newly appearing filename elsewhere on the page does not prove that
    # this input accepted it. Preserve that clue without claiming attachment.
    raise UploadError(
        "upload_unverified",
        "Playwright set the files but exact filename evidence was not observed",
        result=base,
    )


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
