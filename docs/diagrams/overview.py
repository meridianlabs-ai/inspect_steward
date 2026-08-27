"""Generate `overview.excalidraw`, the supervision-stack diagram on the docs home page.

The design (iterated with a design pass against `overview-brief.md`) is four equal-width layer bands — `steward launch`, `steward tend`, the coding agent, the human operator — separated by white connective strips whose labels carry the escalation semantics: every ten minutes unattended, escalates what needs judgement, escalates what needs authority. Colored horizontal rules mark each band's edges while a grey outline closes the stack's sides; it is sized to sit beside prose (bullets to its right on the page), and each band leads with a small line-art icon (rocket, timer, bot, person) drawn from primitives.

Excalidraw limitations accepted: no italic (the connective labels compensate by being smaller and lighter), no bold (title size and color compensate), icons are primitive recreations of the mock's rather than icon-font glyphs.

Regenerate with `python3 docs/diagrams/overview.py`. The `.excalidraw` file is the committed artifact; the SVG next to it is produced at render time by the extension's excalidraw filter and is gitignored.
"""

import json
import math
from pathlib import Path
from typing import Any

UPDATED = 1712345678000

els: list[dict[str, Any]] = []
_n = 0


def _base(t: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
    global _n
    _n += 1
    return {
        "id": f"el_{_n}",
        "type": t,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 0,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": 1000 + _n,
        "version": 1,
        "versionNonce": 2000 + _n,
        "isDeleted": False,
        "updated": UPDATED,
        "link": None,
        "locked": False,
    }


def rect(
    x: float,
    y: float,
    w: float,
    h: float,
    stroke: str,
    bg: str = "transparent",
    stroke_width: int = 1,
    rounded: bool = False,
) -> None:
    e = _base("rectangle", x, y, w, h)
    e.update(
        strokeColor=stroke,
        backgroundColor=bg,
        strokeWidth=stroke_width,
        roundness={"type": 3} if rounded else None,
    )
    els.append(e)


def text(
    x: float,
    y: float,
    s: str,
    size: int,
    color: str,
    font: int = 2,
) -> None:
    per = (0.60 if font == 3 else 0.44) * size
    e = _base("text", x, y, len(s) * per, size * 1.25)
    e.update(
        strokeColor=color,
        text=s,
        fontSize=size,
        fontFamily=font,
        textAlign="left",
        verticalAlign="top",
        baseline=size,
        containerId=None,
        originalText=s,
        autoResize=True,
        lineHeight=1.25,
    )
    els.append(e)


def line(
    pts: list[tuple[float, float]],
    color: str,
    stroke_width: int = 2,
    curved: bool = False,
) -> None:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, y0 = min(xs), min(ys)
    e = _base("line", x0, y0, max(xs) - x0, max(ys) - y0)
    e.update(
        strokeColor=color,
        strokeWidth=stroke_width,
        roundness={"type": 2} if curved else None,
        points=[[px - x0, py - y0] for px, py in pts],
        lastCommittedPoint=None,
        startBinding=None,
        endBinding=None,
        startArrowhead=None,
        endArrowhead=None,
    )
    els.append(e)


def ellipse(
    x: float,
    y: float,
    w: float,
    h: float,
    color: str,
    filled: bool = False,
) -> None:
    e = _base("ellipse", x, y, w, h)
    e.update(
        strokeColor=color,
        backgroundColor=color if filled else "transparent",
        strokeWidth=2,
    )
    els.append(e)


def scale_last(n: int, cx: float, cy: float, f: float) -> None:
    """Scale the last `n` elements as a group about (`cx`, `cy`)."""
    for e in els[-n:]:
        e["x"] = cx + (e["x"] - cx) * f
        e["y"] = cy + (e["y"] - cy) * f
        e["width"] *= f
        e["height"] *= f
        if e.get("points"):
            e["points"] = [[px * f, py * f] for px, py in e["points"]]


def shift_last(n: int, dx: float, dy: float = 0.0) -> None:
    """Move the last `n` elements as a group."""
    for e in els[-n:]:
        e["x"] += dx
        e["y"] += dy


def rotate_last(n: int, cx: float, cy: float, theta: float) -> None:
    """Rotate the last `n` elements as a group about (`cx`, `cy`)."""
    for e in els[-n:]:
        ex = e["x"] + e["width"] / 2
        ey = e["y"] + e["height"] / 2
        dx, dy = ex - cx, ey - cy
        e["x"] = cx + dx * math.cos(theta) - dy * math.sin(theta) - e["width"] / 2
        e["y"] = cy + dx * math.sin(theta) + dy * math.cos(theta) - e["height"] / 2
        e["angle"] = e["angle"] + theta


# ---------------------------------------------------------------- palette

INDIGO = "#3730a3"  # steward titles
INDIGO_ICON = "#4f46e5"
INDIGO_RULE = "#8b95f2"
PURPLE = "#7c3aed"  # agent title
PURPLE_ICON = "#8b5cf6"
PURPLE_RULE = "#c4b5fd"
AMBER = "#b45309"  # human title
AMBER_ICON = "#f59e0b"
AMBER_RULE = "#ecc94b"
SUBTITLE = "#374151"
CONNECTIVE = "#9ca3af"

# ----------------------------------------------------------------- layout

STACK_X = 76
STACK_W = 390
BAND_H = 64
CONN_H = 30
TEXT_X = 122

BANDS: list[dict[str, Any]] = [
    {
        "title": "steward launch",
        "font": 3,
        "color": INDIGO,
        "fill": "#e4e9fc",
        "rule": INDIGO_RULE,
        "subtitle": "execute eval set and schedule automated monitoring",
        "icon": "rocket",
    },
    {
        "title": "steward tend",
        "font": 3,
        "color": INDIGO,
        "fill": "#e4e9fc",
        "rule": INDIGO_RULE,
        "subtitle": "respawn failures, report status, send notifications",
        "icon": "timer",
    },
    {
        "title": "coding agent",
        "font": 2,
        "color": PURPLE,
        "fill": "#f2ecfd",
        "rule": PURPLE_RULE,
        "subtitle": "diagnose and recover from errors, tune concurrency",
        "icon": "bot",
    },
    {
        "title": "human operator",
        "font": 2,
        "color": AMBER,
        "fill": "#fdf7dc",
        "rule": AMBER_RULE,
        "subtitle": "approve tool calls, rule on ambiguous cases, sign off",
        "icon": "person",
    },
]

CONNECTIVES = [
    "then every ten minutes, unattended",
    "escalates what needs judgement",
    "escalates what needs authority",
]

# ------------------------------------------------------------------ icons


def icon_rocket(cy: float) -> None:
    c = INDIGO_ICON
    line([(104, cy - 8), (110, cy - 16), (116, cy - 8)], c)  # nose
    rect(104, cy - 8, 12, 16, c, stroke_width=2, rounded=True)  # body
    ellipse(107, cy - 3, 6, 6, c)  # window
    line([(104, cy + 4), (98, cy + 12)], c)  # left fin
    line([(116, cy + 4), (122, cy + 12)], c)  # right fin
    rotate_last(5, 110, cy - 2, math.pi / 4)  # in flight, up and to the right
    scale_last(5, 110, cy - 2, 0.8)
    shift_last(5, -10)


def icon_timer(cy: float) -> None:
    c = INDIGO_ICON
    line([(106, cy - 14), (114, cy - 14)], c)  # button
    line([(110, cy - 14), (110, cy - 10)], c)  # stem
    ellipse(100, cy - 10, 20, 20, c)  # face
    line([(110, cy), (115, cy - 6)], c)  # hand
    scale_last(4, 110, cy - 2, 0.8)
    shift_last(4, -10)


def icon_bot(cy: float) -> None:
    c = PURPLE_ICON
    line([(110, cy - 14), (110, cy - 8)], c)  # antenna
    rect(99, cy - 8, 22, 16, c, stroke_width=2, rounded=True)  # head
    ellipse(104, cy - 2, 3, 3, c, filled=True)  # left eye
    ellipse(113, cy - 2, 3, 3, c, filled=True)  # right eye
    scale_last(4, 110, cy - 3, 0.8)
    shift_last(4, -10)


def icon_person(cy: float) -> None:
    c = AMBER_ICON
    ellipse(105, cy - 12, 10, 10, c)  # head
    line([(101, cy + 12), (110, cy + 1), (119, cy + 12)], c, curved=True)  # shoulders
    scale_last(2, 110, cy, 0.8)
    shift_last(2, -10)


ICONS = {
    "rocket": icon_rocket,
    "timer": icon_timer,
    "bot": icon_bot,
    "person": icon_person,
}

# ------------------------------------------------------------------ scene

# band fills first, then per-band rules, then the grey outline over both
y = 20.0
band_ys: list[float] = []
for band in BANDS:
    band_ys.append(y)
    rect(STACK_X, y, STACK_W, BAND_H, band["fill"], band["fill"])
    y += BAND_H + CONN_H

for band, by in zip(BANDS, band_ys):
    line([(STACK_X, by), (STACK_X + STACK_W, by)], band["rule"], 1)
    line([(STACK_X, by + BAND_H), (STACK_X + STACK_W, by + BAND_H)], band["rule"], 1)

stack_h = len(BANDS) * BAND_H + len(CONNECTIVES) * CONN_H
rect(STACK_X, 20, STACK_W, stack_h, "#d1d5db")

for i, (band, by) in enumerate(zip(BANDS, band_ys)):
    ICONS[band["icon"]](by + BAND_H / 2)
    # the code font renders larger at equal point size; 18 matches sans 20
    title_size = 18 if band["font"] == 3 else 20
    text(TEXT_X, by + 8, band["title"], title_size, band["color"], font=band["font"])
    text(TEXT_X, by + 36, band["subtitle"], 14, SUBTITLE)
    if i < len(CONNECTIVES):
        text(TEXT_X, by + BAND_H + 7, CONNECTIVES[i], 13, CONNECTIVE)

doc = {
    "type": "excalidraw",
    "version": 2,
    "source": "inspect_steward docs",
    "elements": els,
    "appState": {"gridSize": 20, "viewBackgroundColor": "#ffffff"},
    "files": {},
}
out = Path(__file__).parent / "overview.excalidraw"
out.write_text(json.dumps(doc, indent=1) + "\n")
print(f"wrote {out}: {len(els)} elements")
