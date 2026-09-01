"""`analysis.md` — the one document neither party writes alone.

**Two authors, one file, and a contract that keeps them apart.** `status.md` and `anomalies.md` are Steward's and are rewritten whole every turn; `AGENTS.md` and `_steward.yaml` are the human's and are never touched. This one is co-authored (workflow.md §12.7): under each task's heading Steward keeps a **facts block** current between a pair of HTML-comment markers, and every word outside those markers is somebody's investigation — quoted, argued, and regenerable from nothing.

**So the merge is the module.** Rendering a document is easy; the hard part is rewriting part of a file whose other part is work, and every clause of the contract below exists to stop one specific loss:

- A task with **no section** gets one appended — heading, facts, and an empty placeholder that says where to write.
- A task **with** a section has only the text *between its markers* replaced. Every other byte of the file comes back unchanged — its line endings and its trailing whitespace included, which is why the merge splits on `keepends` rather than re-joining bare lines on a newline of its own choosing.
- A section whose markers **do not pair** is left entirely alone and the damage goes to `steward.log`. Never guess at a boundary in a file whose other half is somebody's work.
- A section for a task the manifest **no longer names** is left alone. The file is durable; a removed task's investigation is still what happened.

**Keyed on the identifier, headed by the display key.** The marker carries the task identifier — stable, content-hashed — while the heading carries the readable key, which is the same split `anomalies_md`'s `keys` mapping already makes. Renaming a task in the definition therefore does not orphan its section.

**What counts as written.** Prose is the non-comment, non-blank text inside a section and outside the markers. HTML comments do not count, which is what lets the placeholder be a prompt rather than an answer — *looked, nothing here* is the deliverable (§12.7), and a placeholder that satisfied the check would let the deliverable go unwritten forever.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .items import anomaly_summary

if TYPE_CHECKING:
    from .turn import TendResult

BEGIN = "<!-- steward:begin {identifier} -->"
END = "<!-- steward:end -->"
"""The facts block's delimiters. HTML comments, so a rendered page shows the facts and not the machinery — and so the file reads as a document rather than as a format."""

_BEGIN = re.compile(r"^<!--\s*steward:begin\s+(\S+)\s*-->\s*$")
_END = re.compile(r"^<!--\s*steward:end\s*-->\s*$")
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")

PLACEHOLDER = (
    "<!-- What do these numbers mean? Write it here. Steward rewrites only "
    "what is between its markers above; everything else in this section is "
    "yours. -->"
)
"""What a fresh section carries instead of prose.

A comment rather than a sentence, on purpose twice over: it is invisible in a rendered document, and it does not read as *written* to the check below — which is what keeps the `unwritten` item on until somebody actually answers it.
"""

HEADER = (
    "<!-- Co-authored. Steward keeps the facts between its markers current "
    "every turn; everything outside them is yours and is never touched. -->"
)
"""The banner, and deliberately not `status.md`'s. That one says *edits are lost*, which here would be exactly wrong."""

INTRO = (
    "What these numbers mean. One section per task: the facts Steward keeps "
    "current, and the reading of them that only a person can write."
)


@dataclass(frozen=True)
class Section:
    """One task's facts, ready to be merged into whatever the file already says."""

    identifier: str
    """The task identifier — what the marker is keyed on, and what survives a rename."""

    key: str
    """The display key, which is what the heading says."""

    facts: tuple[str, ...] = ()
    """The block's bullets, each without its leading marker."""


@dataclass(frozen=True)
class Merged:
    """The result of folding this turn's facts into the file as it stood."""

    body: str
    """The whole document, to be written as-is."""

    unwritten: dict[str, str] = field(default_factory=dict[str, str])
    """Identifier to display key, for every section carrying no prose — what `items.UNWRITTEN` raises."""

    damaged: tuple[str, ...] = ()
    """Identifiers whose markers did not pair, and which were therefore left exactly as they were."""


def analysis_sections(result: "TendResult") -> list[Section]:
    """This turn's facts, one section per observed task, in the table's order.

    **Only tasks that have landed a log.** A task the fleet has not started has no numbers, so there is nothing to keep current and nothing to ask anybody to explain — and a section appended the moment a manifest names a task would put an `unwritten` item on every run from its first turn, which is how an attention list stops being read. The section arrives with the task's first log and stays for as long as the file does.

    Everything else here is already on the result — the census's windows and their rulings, and the coverage fold — so the document costs a render and no reads at all. A result assembled by hand read no directory and so names no tasks.
    """
    observed = result.observed
    if observed is None:
        return []
    return [
        Section(
            identifier=task.identifier,
            key=task.key,
            facts=tuple(_facts(result, task.identifier)),
        )
        for task in observed.tasks
        if task.current is not None
    ]


def _facts(result: "TendResult", identifier: str) -> list[str]:
    """One task's bullets: what was looked at, what was found, and what was decided.

    **The class lines are shared wording**, from `items.anomaly_summary`, so the decision queue and this file cannot describe one finding two ways. A class spanning more than one task says so, because the count in that sentence is the window's and the window is run-wide — a per-task number nothing computes would be the alternative, and a count that is quietly the wrong scope is worse than one that names its scope.
    """
    facts: list[str] = []
    if (scanned := result.coverage.by_task.get(identifier)) is not None:
        if not scanned.known:
            # this file is the one somebody quotes into a report, so the bullet
            # that cannot be substantiated says so rather than saying zero
            facts.append(
                f"could not establish how many of {scanned.landed} transcripts "
                "were scanned — this task's current log would not read"
            )
        else:
            facts.append(
                f"scanned {scanned.scanned} of {scanned.landed} transcripts"
                + (
                    ""
                    if scanned.complete
                    else " — the rest carry no verdict either way"
                )
            )
    windows = [
        anomaly
        for anomaly in (*result.anomalies.open, *result.anomalies.settled)
        if identifier in anomaly.evidence.tasks
    ]
    unruled: list[int] = []
    for anomaly in sorted(windows, key=lambda one: (one.class_key, one.generation)):
        spans = len(anomaly.evidence.tasks)
        where = f" (in {spans} tasks)" if spans > 1 else ""
        ruling = anomaly.ruling
        if ruling is None:
            decided = "no ruling yet"
            unruled.append(anomaly.evidence.count)
        else:
            # the reason verbatim and quoted, which is §12.7's whole claim about
            # this file: the number is in the table above and the sentence
            # somebody wrote about it is the part that cannot be recomputed
            decided = f'{ruling.disposition.value} by {ruling.by} — "{ruling.reason}"'
        facts.append(f"{anomaly_summary(anomaly)}{where}; {decided}")
    if len(unruled) > 1:
        # **the unprobed count, and only where it is a summary rather than an
        # echo.** With one open class the line above already carries the number
        # and says nobody has ruled it; repeating that as a total is a second
        # sentence saying the first one again
        awaiting = sum(unruled)
        facts.append(
            f"{awaiting} sample{'' if awaiting == 1 else 's'} across "
            f"{len(unruled)} classes nobody has ruled yet"
        )
    if not facts:
        facts.append("nothing flagged, nothing errored")
    return facts


def merge_analysis(existing: str, sections: Sequence[Section]) -> Merged:
    """Fold this turn's facts into the file as it stands, touching nothing else.

    Args:
        existing: The file's current contents, or empty where there is no file yet.
        sections: This turn's facts, from `analysis_sections`.

    Returns:
        The whole document to write, the sections carrying no prose, and any whose markers did not pair. An empty body means *write nothing*: no file yet and no task has landed anything, so a document saying so would be a file created for a run with nothing in it.
    """
    if not existing.strip() and not sections:
        return Merged(body="")
    # **`keepends` is what makes *byte-identical* true rather than nearly true.**
    # Splitting a file into bare lines and re-joining them on `"\n"` silently
    # rewrites every line ending of a CRLF file and eats whatever trailed the
    # last one -- which is not a merge touching only what is between the
    # markers, it is whole-file churn in a document under version control. With
    # the terminators kept on the lines they belong to, `"".join` reproduces
    # every byte this function did not deliberately replace
    newline = _newline(existing)
    lines = (
        existing.splitlines(keepends=True) if existing.strip() else _preamble(newline)
    )
    spans, damaged = _spans(lines)
    # back to front, so an earlier replacement cannot move a later span's index
    for section in sorted(
        (one for one in sections if one.identifier in spans),
        key=lambda one: spans[one.identifier][0],
        reverse=True,
    ):
        start, finish = spans[section.identifier]
        lines[start + 1 : finish] = _bullets(section, newline)
    fresh = [
        section
        for section in sections
        if section.identifier not in spans and section.identifier not in damaged
    ]
    if fresh and lines and not lines[-1].endswith(("\n", "\r")):
        # a file that did not end in a newline gets one rather than having a
        # heading welded onto its last line
        lines[-1] += newline
    for section in fresh:
        lines += _section(section, newline)
    body = "".join(lines)
    return Merged(
        body=body,
        unwritten=_unwritten(body, sections, damaged),
        damaged=tuple(damaged),
    )


def _newline(existing: str) -> str:
    """The line ending to write new lines with — the file's own, where it has one.

    A file holding any CRLF is treated as a CRLF file, which is the reading that keeps a Windows checkout from acquiring mixed endings one turn at a time. A new file, or one with no line endings at all, gets a bare newline.
    """
    return "\r\n" if "\r\n" in existing else "\n"


def _preamble(newline: str) -> list[str]:
    return [line + newline for line in ("# analysis", "", HEADER, "", INTRO, "")]


def _section(section: Section, newline: str) -> list[str]:
    """A section this file has never had: heading, facts, and the prompt to answer."""
    return [
        f"## {section.key}{newline}",
        newline,
        BEGIN.format(identifier=section.identifier) + newline,
        *_bullets(section, newline),
        END + newline,
        newline,
        PLACEHOLDER + newline,
        newline,
    ]


def _bullets(section: Section, newline: str) -> list[str]:
    return [f"- {fact}{newline}" for fact in section.facts]


def _spans(lines: list[str]) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Where each task's markers are, and which ones do not pair.

    A begin followed by another begin, a begin the file ends on, or two blocks claiming one identifier are all **damage**: the file is somebody's work and a boundary this cannot be certain of is a boundary it must not write across. A stray end marker with no begin names no task and so damages nothing — it is simply not a block.
    """
    spans: dict[str, tuple[int, int]] = {}
    damaged: list[str] = []
    opened: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        if (matched := _BEGIN.match(line)) is not None:
            if opened is not None:
                damaged.append(opened[0])
            opened = (matched.group(1), index)
        elif _END.match(line) is not None and opened is not None:
            identifier, start = opened
            if identifier in spans:
                damaged.append(identifier)
            else:
                spans[identifier] = (start, index)
            opened = None
    if opened is not None:
        damaged.append(opened[0])
    for identifier in damaged:
        spans.pop(identifier, None)
    return spans, list(dict.fromkeys(damaged))


def _unwritten(
    body: str, sections: Sequence[Section], damaged: Sequence[str]
) -> dict[str, str]:
    """Every section of the merged document that carries no prose.

    Read back off the body rather than tracked through the merge, because that is the question being asked: *does this file, as it now stands, say what the numbers mean* — and a section somebody wrote two turns ago must answer it as surely as one written this turn.

    A **damaged** section is never reported. Its markers did not pair, so nothing here knows where it begins or ends, and *you have not written this* is a claim this cannot make about text it declined to read.
    """
    # `keepends` to match what the merge splits on, so the two readings of one
    # document cannot disagree about where a marker is
    lines = body.splitlines(keepends=True)
    spans, _ = _spans(lines)
    keys = {section.identifier: section.key for section in sections}
    return {
        identifier: keys[identifier]
        for identifier, span in spans.items()
        if identifier in keys and identifier not in damaged and not _prose(lines, span)
    }


def _prose(lines: list[str], span: tuple[int, int]) -> str:
    """A section's own text: everything under its heading and outside its markers.

    Blank lines and HTML comments do not count. The comment rule is what lets the placeholder be a prompt — a placeholder that read as prose would answer the question it was asking.
    """
    start, finish = span
    head = start
    while head > 0 and not lines[head - 1].startswith("## "):
        head -= 1
    tail = finish + 1
    while tail < len(lines) and not lines[tail].startswith("## "):
        tail += 1
    outside = [*lines[head:start], *lines[finish + 1 : tail]]
    return "\n".join(
        line for line in outside if line.strip() and not _COMMENT.match(line)
    ).strip()


__all__ = [
    "BEGIN",
    "END",
    "HEADER",
    "PLACEHOLDER",
    "Merged",
    "Section",
    "analysis_sections",
    "merge_analysis",
]
