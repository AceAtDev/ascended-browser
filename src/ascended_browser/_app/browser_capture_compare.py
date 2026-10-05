"""Pictures compared and composed for a developer's eye.

Two captures of the same viewport before and after a CSS edit differ in a
few hundred pixels a model cannot find by looking at two images. Pillow can:
``diff_captures`` returns the changed share, the regions that changed, and
one picture (before, after with the regions boxed, the change mask) so the
answer is numbers first and a picture second. ``compose_grid`` lays several
captures side by side with labels: the phone, tablet and desktop views of a
page in one evidence file instead of three.

Both work on PNG/JPEG bytes and return PNG bytes; neither touches a page.
"""
from __future__ import annotations

import io
from typing import Any, Iterable

_RED = (220, 38, 38)
_LABEL_HEIGHT = 22
_GUTTER = 12
_MAX_REGIONS = 12


def _open(data: bytes):
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    return image.convert("RGB")


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _font():
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=13)
    except TypeError:  # older Pillow without size
        return ImageFont.load_default()


def compose_grid(
    tiles: Iterable[tuple[str, bytes]], *, columns: int | None = None,
    max_width: int = 1600, max_tile_height: int = 720,
) -> tuple[bytes, dict[str, Any]]:
    """Several labelled captures in one PNG, ``columns`` tiles per row.

    Tiles in a row share a height, and a row wider than ``max_width`` is scaled
    down to fit: a phone, tablet and desktop side by side stay legible once
    the picture is downscaled for the model, instead of becoming one strip.
    """
    from PIL import Image, ImageDraw

    items = [(str(label), _open(data)) for label, data in tiles]
    if not items:
        raise ValueError("compose_grid needs at least one picture")
    columns = max(1, min(columns or len(items), len(items)))
    rows: list[list[tuple[str, Any]]] = []
    for start in range(0, len(items), columns):
        row = items[start:start + columns]
        tile_height = min(max(image.height for _label, image in row), max_tile_height)
        scaled = [
            (label, image if image.height == tile_height else image.resize(
                (max(1, round(image.width * tile_height / image.height)), tile_height), Image.LANCZOS,
            ))
            for label, image in row
        ]
        gutters = (len(scaled) + 1) * _GUTTER
        content = sum(image.width for _label, image in scaled)
        if content + gutters > max_width:
            factor = max(1, max_width - gutters) / content
            height = max(1, round(tile_height * factor))
            scaled = [
                (label, image.resize((max(1, round(image.width * factor)), height), Image.LANCZOS))
                for label, image in scaled
            ]
        rows.append(scaled)
    width = max(sum(image.width for _label, image in row) + (len(row) + 1) * _GUTTER for row in rows)
    height = sum(row[0][1].height + _LABEL_HEIGHT for row in rows) + (len(rows) + 1) * _GUTTER
    canvas = Image.new("RGB", (width, height), (245, 245, 247))
    draw = ImageDraw.Draw(canvas)
    font = _font()
    cells = []
    y = _GUTTER
    for row in rows:
        x = _GUTTER
        for label, image in row:
            draw.text((x + 2, y + 3), label, fill=(40, 40, 48), font=font)
            canvas.paste(image, (x, y + _LABEL_HEIGHT))
            draw.rectangle([x - 1, y + _LABEL_HEIGHT - 1, x + image.width, y + _LABEL_HEIGHT + image.height], outline=(200, 200, 206))
            cells.append({"label": label, "x": x, "y": y + _LABEL_HEIGHT, "width": image.width, "height": image.height})
            x += image.width + _GUTTER
        y += row[0][1].height + _LABEL_HEIGHT + _GUTTER
    return _png(canvas), {"width": width, "height": height, "tiles": cells}


def _regions(mask, *, cell: int) -> list[dict[str, Any]]:
    """Coarse grid cells with change, merged into row runs then stacked rectangles."""
    width, height = mask.size
    pixels = mask.load()
    cols = (width + cell - 1) // cell
    rows = (height + cell - 1) // cell
    hot = [[False] * cols for _ in range(rows)]
    counts = [[0] * cols for _ in range(rows)]
    for y in range(height):
        row = y // cell
        for x in range(width):
            if pixels[x, y]:
                counts[row][x // cell] += 1
    for r in range(rows):
        for c in range(cols):
            hot[r][c] = counts[r][c] > 0
    # Connected components over the cell grid (4-neighbour), bounding boxes.
    seen = [[False] * cols for _ in range(rows)]
    boxes: list[tuple[int, int, int, int, int]] = []
    for r in range(rows):
        for c in range(cols):
            if not hot[r][c] or seen[r][c]:
                continue
            stack = [(r, c)]
            seen[r][c] = True
            r0 = r1 = r
            c0 = c1 = c
            changed = 0
            while stack:
                cr, cc = stack.pop()
                changed += counts[cr][cc]
                r0, r1, c0, c1 = min(r0, cr), max(r1, cr), min(c0, cc), max(c1, cc)
                for nr, nc in ((cr - 1, cc), (cr + 1, cc), (cr, cc - 1), (cr, cc + 1)):
                    if 0 <= nr < rows and 0 <= nc < cols and hot[nr][nc] and not seen[nr][nc]:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            boxes.append((c0 * cell, r0 * cell, min(width, (c1 + 1) * cell), min(height, (r1 + 1) * cell), changed))
    boxes.sort(key=lambda b: b[4], reverse=True)
    out = []
    for x0, y0, x1, y1, changed in boxes[:_MAX_REGIONS]:
        area = max(1, (x1 - x0) * (y1 - y0))
        out.append({"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0,
                    "changed_pixels": changed, "density": round(changed / area, 3)})
    return out


def diff_captures(before: bytes, after: bytes, *, threshold: int = 24, cell: int = 32) -> tuple[bytes, dict[str, Any]]:
    """Compare two captures of the same size.

    Returns one PNG (before | after with changed regions boxed | mask) and
    ``{changed_percent, changed_pixels, regions, width, height}``. Raises
    ValueError when the sizes differ: a diff of two layouts is not a diff.
    """
    from PIL import Image, ImageChops, ImageDraw

    a, b = _open(before), _open(after)
    if a.size != b.size:
        raise ValueError(
            f"the pictures are different sizes ({a.width}x{a.height} vs {b.width}x{b.height}); "
            "compare two captures with the same scope and viewport"
        )
    delta = ImageChops.difference(a, b).convert("L")
    mask = delta.point(lambda v: 255 if v > threshold else 0)
    changed = sum(1 for v in mask.getdata() if v)
    total = a.width * a.height
    regions = _regions(mask, cell=cell) if changed else []
    boxed = b.copy()
    draw = ImageDraw.Draw(boxed)
    for region in regions:
        draw.rectangle(
            [region["x"], region["y"], region["x"] + region["width"] - 1, region["y"] + region["height"] - 1],
            outline=_RED, width=2,
        )
    panels = [("before", a), ("after (changes boxed)", boxed), ("change mask", mask.convert("RGB"))]
    composite, _layout = compose_grid([(label, _png(img)) for label, img in panels])
    stats = {
        "changed_percent": round(100.0 * changed / total, 3) if total else 0.0,
        "changed_pixels": changed,
        "regions": regions,
        "width": a.width,
        "height": a.height,
        "identical": changed == 0,
    }
    return composite, stats


def describe_diff(stats: dict[str, Any], *, against: str) -> str:
    if stats.get("identical"):
        return f"Identical to {against}: no pixel differs by more than the threshold."
    regions = stats.get("regions") or []
    head = (
        f"Compared with {against}: {stats.get('changed_percent')}% of pixels changed "
        f"({stats.get('changed_pixels')} of {stats.get('width')}x{stats.get('height')}), "
        f"in {len(regions)} region{'s' if len(regions) != 1 else ''}"
    )
    if not regions:
        return head + "."
    parts = [
        f"{r['width']}x{r['height']} at ({r['x']},{r['y']})" for r in regions[:6]
    ]
    return head + ": " + "; ".join(parts) + ("; …" if len(regions) > 6 else "") + ". The attached picture shows before, after with the regions boxed in red, and the change mask."
