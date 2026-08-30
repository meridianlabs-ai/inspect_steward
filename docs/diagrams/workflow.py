"""Build the Steward workflow diagram (the Workflow section of the docs landing page).

Companion to overview.py, which builds the supervision-stack diagram at the top of
the page. The two share a palette and type conventions:

    indigo   both Steward layers (the deterministic machine) and every stage of the spine
    violet   the coding agent
    amber    the human operator
    grey     all connective tissue, plus the run frame

    monospace   things you type (the four commands)
    sans-serif  parties who act, and every description

Usage:
    python workflow.py workflow.excalidraw

Text is laid out with an advance-width model rather than a real font metric, so a
wrapped line can end up a pixel or two narrower than Excalidraw would measure it.
Nothing is bound to a container, so opening the file and nudging a label is safe.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- palette

INDIGO_FILL = "#e4e9fc"
INDIGO_SOFT = "#eef1fd"
INDIGO_GHOST = "#f4f6fe"
INDIGO_LINE = "#c7cffb"
INDIGO_GHOST_LINE = "#dfe4fc"
INDIGO_STRONG = "#8b95f2"
INDIGO_TEXT = "#3730a3"
INDIGO_MONO = "#4f46e5"

VIOLET_FILL = "#f2ecfd"
VIOLET_LINE = "#c4b5fd"
VIOLET_TEXT = "#7c3aed"

AMBER_FILL = "#fdf7dc"
AMBER_LINE = "#ecc94b"
AMBER_TEXT = "#b45309"

RUN_FILL = "#f9fafb"
FRAME_LINE = "#e5e7eb"
CONNECTIVE = "#d1d5db"

BODY = "#374151"
MUTED = "#6b7280"
FAINT = "#9ca3af"
WHITE = "#ffffff"
NONE = "transparent"

SANS, MONO = 2, 3  # Excalidraw font ids: 1 hand-drawn, 2 Helvetica, 3 code

# ---------------------------------------------------------------- geometry

# no outer frame: the diagram floats on the page, so the canvas is exactly the
# content width and the card widths below are unchanged from the framed version
CANVAS_W = 956
FRAME_PAD = 0
GAP = 32          # between cards, horizontally
ROW_GAP = 28      # between rows, taken up by a chevron
CARD_PAD = 16
LINE_H = 1.25

# the three column widths of the middle row, and the two of the outer rows
INNER_W = CANVAS_W - 2 * FRAME_PAD
PAIR_W = (INNER_W - GAP) // 2
RUN_W = 300
ACTOR_W = (INNER_W - RUN_W - 2 * GAP) // 2

# the actor pair starts level with the top of the steward tend box, not with
# the top of the run frame
ACTOR_DROP = 52

# advance width as a fraction of font size, averaged over lowercase prose
ADVANCE = {SANS: 0.515, MONO: 0.600}


def measure(s: str, size: float, font: int) -> float:
    return len(s) * size * ADVANCE[font]


def wrap(s: str, size: float, font: int, max_w: float) -> list[str]:
    words, lines, line = s.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if line and measure(trial, size, font) > max_w:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines or [""]


# ---------------------------------------------------------------- elements


def _base(kind: str, x: float, y: float, w: float, h: float) -> dict:
    return {
        "id": f"{kind}-{random.getrandbits(48):012x}",
        "type": kind,
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(w, 2),
        "height": round(h, 2),
        "angle": 0,
        "strokeColor": CONNECTIVE,
        "backgroundColor": NONE,
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 0,  # house style: clean lines, no sketch roughness
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 3},
        "seed": random.getrandbits(31),
        "version": 1,
        "versionNonce": random.getrandbits(31),
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
    }


def rect(x, y, w, h, fill=NONE, stroke=FRAME_LINE, stroke_w=1, radius=8) -> dict:
    el = _base("rectangle", x, y, w, h)
    el["backgroundColor"] = fill
    el["strokeColor"] = stroke
    el["strokeWidth"] = stroke_w
    el["roundness"] = {"type": 3} if radius else None
    return el


def label(x, y, s, size, font=SANS, color=BODY, max_w=None) -> tuple[dict, float]:
    lines = wrap(s, size, font, max_w) if max_w else [s]
    text = "\n".join(lines)
    w = max(measure(line, size, font) for line in lines)
    h = len(lines) * size * LINE_H
    el = _base("text", x, y, w, h)
    el["strokeColor"] = color
    el["fontSize"] = size
    el["fontFamily"] = font
    el["text"] = text
    el["originalText"] = s
    el["textAlign"] = "left"
    el["verticalAlign"] = "top"
    el["containerId"] = None
    el["lineHeight"] = LINE_H
    # the SVG exporter derives each line's y as (i+1)*lineHeightPx - (height - baseline),
    # so baseline is the top-to-last-line-baseline distance. Absent, every y exports NaN.
    el["baseline"] = h - size * (LINE_H - 1)
    el["roundness"] = None
    return el, h


def arrow(x, y, dx, dy, color=CONNECTIVE, stroke_w=1, head="arrow", tail=None) -> dict:
    el = _base("arrow", x, y, abs(dx), abs(dy))
    el["strokeColor"] = color
    el["strokeWidth"] = stroke_w
    el["points"] = [[0, 0], [round(dx, 2), round(dy, 2)]]
    el["lastCommittedPoint"] = None
    el["startBinding"] = None
    el["endBinding"] = None
    el["startArrowhead"] = tail
    el["endArrowhead"] = head
    el["roundness"] = {"type": 2}
    return el


# ---------------------------------------------------------------- grouping


@dataclass
class Group:
    """Elements in a local coordinate space, plus the height they occupy."""

    els: list = field(default_factory=list)
    h: float = 0.0

    def add(self, *els: dict) -> "Group":
        self.els.extend(els)
        return self

    def merge(self, other: "Group") -> "Group":
        self.els.extend(other.els)
        return self

    def shift(self, dx: float, dy: float) -> "Group":
        for el in self.els:
            el["x"] = round(el["x"] + dx, 2)
            el["y"] = round(el["y"] + dy, 2)
        return self


class Stack:
    """Vertical cursor. Blocks are appended with an explicit gap above each."""

    def __init__(self, width: float):
        self.w = width
        self.y = 0.0
        self.group = Group()

    def text(self, s, size, font=SANS, color=BODY, gap=0, wrapped=True):
        self.y += gap
        el, h = label(0, self.y, s, size, font, color, self.w if wrapped else None)
        self.group.add(el)
        self.y += h
        return self

    def block(self, g: Group, gap=0):
        self.y += gap
        self.group.merge(g.shift(0, self.y))
        self.y += g.h
        return self

    def done(self) -> Group:
        self.group.h = self.y
        return self.group


# ---------------------------------------------------------------- parts


def chips(names: list[str], width: float, size=12, font=MONO,
          color=INDIGO_MONO, pad_x=8, pad_y=3, gap=6) -> Group:
    """A wrapping row of small monospace artifact chips on a white ground."""
    g, x, y, row_h = Group(), 0.0, 0.0, 0.0
    for name in names:
        w = measure(name, size, font) + 2 * pad_x
        h = size * LINE_H + 2 * pad_y
        if x and x + w > width:
            x, y = 0.0, y + h + gap
        g.add(rect(x, y, w, h, WHITE, INDIGO_LINE, 1, radius=5))
        el, _ = label(x + pad_x, y + pad_y, name, size, font, color)
        g.add(el)
        x += w + gap
        row_h = y + h
    g.h = row_h
    return g


def connective(text: str, width: float) -> Group:
    """A short downward arrow and its grey label, between boxes inside the run."""
    g = Group()
    g.add(arrow(4, 2, 0, 12, CONNECTIVE, 1))
    el, h = label(20, 0, text, 13, SANS, FAINT)
    g.add(el)
    g.h = max(h, 16)
    return g


def escalation(text: str, width: float, outbound: bool) -> Group:
    """One of the two 'receives' / 'returns' lines inside an actor card."""
    g = Group()
    g.add(arrow(0, 6, -8 if outbound else 8, 0, FAINT, 1))
    indent = 15
    el, h = label(indent, 0, text, 13, SANS, BODY, width - indent)
    g.add(el)
    g.h = h
    return g


def command_card(width: float, cmd: str, desc: str,
                 chip_names: list[str] | None = None,
                 aside: tuple[str, str] | None = None,
                 min_h: float | None = None) -> Group:
    inner = width - 2 * CARD_PAD
    s = Stack(inner)
    s.text(cmd, 18, MONO, INDIGO_TEXT, wrapped=False)
    s.text(desc, 14, SANS, BODY, gap=5)
    if chip_names:
        s.block(chips(chip_names, inner), gap=11)
    if aside:
        flag, note = aside
        g = Group()
        el, _ = label(0, 0, flag, 12, MONO, INDIGO_MONO)
        g.add(el)
        x = measure(flag, 12, MONO) + 8
        el, h = label(x, 0, note, 13, SANS, MUTED, inner - x)
        g.add(el)
        g.h = h
        s.block(g, gap=11)
    return frame_card(width, s.done(), INDIGO_FILL, INDIGO_LINE, 1, min_h)


def stage_card(width: float, title: str, desc: str, fill: str, line: str,
               title_color: str, min_h: float | None = None) -> Group:
    inner = width - 2 * CARD_PAD
    s = Stack(inner)
    s.text(title, 20, SANS, title_color, wrapped=False)
    s.text(desc, 14, SANS, BODY, gap=5)
    return frame_card(width, s.done(), fill, line, 1, min_h)


def actor_card(width: float, title: str, desc: str, receives: str, returns: str,
               fill: str, line: str, title_color: str,
               min_h: float | None = None) -> Group:
    inner = width - 2 * CARD_PAD
    s = Stack(inner)
    s.text(title, 20, SANS, title_color, wrapped=False)
    s.text(desc, 14, SANS, BODY, gap=5)
    # a hairline rule, then the received / returned pair
    rule = Group()
    rule.add(rect(0, 0, inner, 0, NONE, line, 1, radius=0))
    rule.h = 0
    s.block(rule, gap=13)
    s.block(escalation(receives, inner, outbound=False), gap=12)
    s.block(escalation(returns, inner, outbound=True), gap=10)
    return frame_card(width, s.done(), fill, line, 1, min_h)


def frame_card(width: float, body: Group, fill: str, line: str,
               stroke_w: float = 1, min_h: float | None = None,
               pad_top: float = 15, pad_bottom: float = 17) -> Group:
    h = pad_top + body.h + pad_bottom
    if min_h is not None:
        h = max(h, min_h)
    g = Group()
    g.add(rect(0, 0, width, h, fill, line, stroke_w))
    g.merge(body.shift(CARD_PAD, pad_top))
    g.h = h
    return g


def run_column(width: float) -> Group:
    """The run: steward tend, the task fleet it spawns, the logs results land in."""
    inner = width - 2 * CARD_PAD
    s = Stack(inner)

    head = Group()
    el, h = label(0, 0, "the run", 20, SANS, BODY)
    head.add(el)
    el2, _ = label(measure("the run", 20, SANS) + 10, 6, "unattended", 13, SANS, FAINT)
    head.add(el2)
    head.h = h
    s.block(head)

    # steward tend, the hub: stronger stroke than anything else in the column
    t = Stack(inner - 2 * 15)
    row = Group()
    el, h = label(0, 0, "steward tend", 18, MONO, INDIGO_TEXT)
    row.add(el)
    el2, _ = label(measure("steward tend", 18, MONO) + 9, 5,
                   "every 10 minutes", 13, SANS, MUTED)
    row.add(el2)
    row.h = h
    t.block(row)
    t.block(chips(["observe", "decide", "act", "record"], inner - 30,
                  size=13, font=SANS, color=BODY, pad_x=9), gap=10)
    s.block(frame_card(inner, t.done(), INDIGO_FILL, INDIGO_STRONG, 1.5,
                       pad_top=13, pad_bottom=14), gap=13)

    s.block(connective("spawns and reaps", inner), gap=6)

    # the task fleet: several detached workers, drawn as a stack of cards
    f = Stack(inner - 28)
    f.text("task fleet", 15, SANS, INDIGO_TEXT, wrapped=False)
    f.text("one process per task", 13, SANS, BODY, gap=3)
    face = frame_card(inner, f.done(), INDIGO_SOFT, INDIGO_LINE, 1,
                      pad_top=11, pad_bottom=12)
    fleet = Group()
    fleet.add(rect(12, 8, inner - 24, face.h, INDIGO_GHOST, INDIGO_GHOST_LINE))
    fleet.add(rect(6, 4, inner - 12, face.h, "#f1f4fe", INDIGO_GHOST_LINE))
    fleet.merge(face)
    fleet.h = face.h + 8
    s.block(fleet, gap=6)

    s.block(connective("results land", inner), gap=6)

    lg = Stack(inner - 28)
    lg.text("logs and journal", 15, SANS, INDIGO_TEXT, wrapped=False)
    lg.block(chips(["logs/", "scans/", "journal.jsonl"], inner - 28), gap=10)
    s.block(frame_card(inner, lg.done(), INDIGO_SOFT, INDIGO_LINE, 1,
                       pad_top=11, pad_bottom=12), gap=6)

    return frame_card(width, s.done(), RUN_FILL, FRAME_LINE, 1, pad_bottom=17)


# ---------------------------------------------------------------- assembly


def build() -> list[dict]:
    els: list[dict] = []
    x0, y = FRAME_PAD, FRAME_PAD

    def row(cards: list[Group], xs: list[float], top: float) -> float:
        for card, x in zip(cards, xs):
            els.extend(card.shift(x, top).els)
        return max(c.h for c in cards)

    def chevron(top: float) -> float:
        els.append(arrow(CANVAS_W / 2, top + 8, 0, 12, CONNECTIVE, 1))
        return ROW_GAP

    def between(left_x: float, width: float, top: float, height: float,
                tail: str | None = None) -> None:
        """A horizontal arrow sitting in the gap, centred on the pair's height."""
        els.append(arrow(left_x + width + 6, top + height / 2, GAP - 12, 0,
                         CONNECTIVE, 1, tail=tail))

    # --- steward init → steward launch
    init = command_card(PAIR_W, "steward init", "create the workspace, define the eval set",
                        chip_names=["evalset.py", "_steward.yaml", "journal.jsonl"])
    launch = command_card(PAIR_W, "steward launch",
                          "capture the definition, arm the timer, spawn the tasks",
                          aside=("--smoke", "a bounded rehearsal before the full run"))
    pair_h = max(init.h, launch.h)
    init = command_card(PAIR_W, "steward init", "create the workspace, define the eval set",
                        chip_names=["evalset.py", "_steward.yaml", "journal.jsonl"],
                        min_h=pair_h)
    launch = command_card(PAIR_W, "steward launch",
                          "capture the definition, arm the timer, spawn the tasks",
                          aside=("--smoke", "a bounded rehearsal before the full run"),
                          min_h=pair_h)
    h = row([init, launch], [x0, x0 + PAIR_W + GAP], y)
    between(x0, PAIR_W, y, h)
    y += h + chevron(y + h)

    # --- the run · coding agent · human operator
    run = run_column(RUN_W)
    agent = actor_card(ACTOR_W, "coding agent",
                       "diagnose errors, tune concurrency, investigate",
                       "receives what needs judgement (unclassified errors, "
                       "a stalled ramp, a suspect result)",
                       "collects run observations and takes required action "
                       "(adjust concurrency, restart task or sample, etc.)",
                       VIOLET_FILL, VIOLET_LINE, VIOLET_TEXT)
    human = actor_card(ACTOR_W, "human operator",
                       "rule on error classes, approve tool calls, sign off",
                       "receives what needs authority (an error class the agent "
                       "will not settle, a blocked tool call)",
                       "answers once, and the agent carries the decision out "
                       "across the class",
                       AMBER_FILL, AMBER_LINE, AMBER_TEXT)
    actor_h = max(agent.h, human.h)  # the pair's bottoms line up
    agent = actor_card(ACTOR_W, "coding agent",
                       "diagnose errors, tune concurrency, investigate",
                       "receives what needs judgement (unclassified errors, "
                       "a stalled ramp, a suspect result)",
                       "collects run observations and takes required action "
                       "(adjust concurrency, restart task or sample, etc.)",
                       VIOLET_FILL, VIOLET_LINE, VIOLET_TEXT, min_h=actor_h)
    human = actor_card(ACTOR_W, "human operator",
                       "rule on error classes, approve tool calls, sign off",
                       "receives what needs authority (an error class the agent "
                       "will not settle, a blocked tool call)",
                       "answers once, and the agent carries the decision out "
                       "across the class",
                       AMBER_FILL, AMBER_LINE, AMBER_TEXT, min_h=actor_h)
    els.extend(run.shift(x0, y).els)
    ax = x0 + RUN_W + GAP
    els.extend(agent.shift(ax, y + ACTOR_DROP).els)
    els.extend(human.shift(ax + ACTOR_W + GAP, y + ACTOR_DROP).els)
    h = max(run.h, ACTOR_DROP + actor_h)
    y += h + chevron(y + h)

    # --- adjudication → steward signoff
    adj = stage_card(PAIR_W, "adjudication",
                     "do environment or apparatus problems invalidate samples? "
                     "should errors be re-run, scored as failures, or ignored?",
                     INDIGO_FILL, INDIGO_LINE, INDIGO_TEXT)
    off = command_card(PAIR_W, "steward signoff",
                       "unschedule the monitor, accept the adjudicated results, "
                       "and publish the logs for analysis")
    pair_h = max(adj.h, off.h)
    adj = stage_card(PAIR_W, "adjudication",
                     "do environment or apparatus problems invalidate samples? "
                     "should errors be re-run, scored as failures, or ignored?",
                     INDIGO_FILL, INDIGO_LINE, INDIGO_TEXT, min_h=pair_h)
    off = command_card(PAIR_W, "steward signoff",
                       "unschedule the monitor, accept the adjudicated results, "
                       "and publish the logs for analysis", min_h=pair_h)
    # signoff's title is monospace but the box is indigo like every other stage:
    # violet and amber are reserved for the two parties in the middle row
    h = row([adj, off], [x0, x0 + PAIR_W + GAP], y)
    between(x0, PAIR_W, y, h)
    y += h

    return els


def document(elements: list[dict]) -> dict:
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "workflow.py",
        "elements": elements,
        "appState": {"viewBackgroundColor": WHITE, "gridSize": None},
        "files": {},
    }


def main() -> None:
    random.seed(20260830)  # stable ids, so a rebuild is a no-op in git
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "workflow.excalidraw")
    out.write_text(json.dumps(document(build()), indent=2) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
