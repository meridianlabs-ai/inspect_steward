"""Turning one failure into a class key, so five hundred of them can be one decision.

A night of errored samples is not five hundred facts, it is usually two or three — a provider died, a scorer has a bug, a disk filled — each showing up as a population of identical failures. The class key is what makes the population addressable: `error:{type}@{frame}` groups every sample that failed the same way, and a ruling on the class covers all of them (workflow.md §12.1).

**Message text is never in the key.** A message carries sample ids, temp paths, and timestamps — a splitter wearing a disguise — so identity is the exception's *type* and its *raising frame*, both of which are byte-identical across samples, tasks, hosts, and re-runs when the failure is the same failure. The verbatim message travels beside the key as evidence, display-only.

**The frame carries no line number**, deliberately: an edited-and-relaunched definition shifts every line, and precedent (workflow.md §12.8) should survive an edit that did not touch the failing code path. The path is collapsed to its last two segments because absolute paths carry usernames, hosts, and venv roots — exactly the material a stable key must exclude.

**The deepest frame is the discriminator**, not "the last frame outside inspect's code": that predicate is a moving path heuristic, and when inspect itself is the raiser — sandbox provisioning, limit machinery — the inspect frame is what separates unrelated causes. The known cost is an occasional over-merge of two call sites of one library error, and over-merging is recoverable where over-splitting is noise (the proposal layer exists to collapse splits, and a ruling reads the evidence either way).

Everything here is pure text: no filesystem, no clock, no reads. The read path that feeds it is `instances.py`.
"""

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from hashlib import sha256

# upstream's own convention for sibling-failure teardown: a sample whose error
# is a cancellation repr was killed because something *else* failed, and it is
# not an instance of anything. A private import, the established reach-around
# (`_eval.evalset`, `_control.discovery`)
from inspect_ai._util.error import is_cancellation_message

MESSAGE_CAP = 200
"""Characters of a verbatim error message carried as evidence, the `_MAX_DAMAGE_TEXT` convention."""

UNCLASSED = "error:unclassed"
"""The bucket for a failure nothing could be parsed from.

Over-merges by design — anything landing here is investigation material — and never a message digest, which would be message text in the key wearing a disguise.
"""

_EXCEPTION = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)(?::|$)")
"""A traceback's exception line: a dotted name at column 0, followed by `:` or end-of-line.

`Traceback (most recent call last):` does not match — the space after `Traceback` breaks the pattern — so the marker needs no special-casing. The name must also pass `_exceptional`, because worker-output tails are arbitrary console text where `error: could not resolve model` would otherwise class as an exception named `error`.
"""

_FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (.+)$')

_REPR = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\(")
"""An exception repr, which is what upstream puts in `EvalError.message` by convention."""


@dataclass(frozen=True)
class ParsedError:
    """What one failure's text yields: identity for the key, and the path for the substrate flag."""

    type: str
    """The exception's dotted name as `format_exception` prints it, kept whole."""

    frame: str
    """The raising frame as `{last-two-path-segments}:{func}`, or `unknown`."""

    path: str = ""
    """The raising frame's full path — key material never, substrate material only."""


def parse_error(message: str | None, traceback: str | None) -> ParsedError | None:
    """Identity from a failure's text, or `None` where there is none to parse.

    The traceback is the authority: its final block is the outermost exception — the one that halted the sample — and its deepest frame is where the raise happened. The message is the fallback, useful because upstream records `repr(ex)` by convention, which yields the type and nothing else.

    Args:
        message: The error message, usually `repr(ex)`.
        traceback: The formatted traceback, when one was recorded.

    Returns:
        The parsed identity, or `None` — the caller's `error:unclassed`.
    """
    if traceback:
        lines = traceback.splitlines()
        for index in range(len(lines) - 1, -1, -1):
            matched = _EXCEPTION.match(lines[index])
            if matched is None or not _exceptional(matched.group(1)):
                continue
            frame, path = _raising_frame(lines[:index])
            return ParsedError(type=matched.group(1), frame=frame, path=path)
    if message and (matched := _REPR.match(message)) is not None:
        if _exceptional(matched.group(1)):
            return ParsedError(type=matched.group(1), frame="unknown")
    return None


def _exceptional(name: str) -> bool:
    """Whether a dotted name plausibly names an exception class.

    CapWords is the convention every exception in practice follows, and requiring one uppercase letter in the final segment is what keeps prose like `error:` or `warning:` — ordinary in the console tails this parser is also pointed at — from minting junk classes.
    """
    return any(char.isupper() for char in name.rsplit(".", 1)[-1])


def _raising_frame(lines: list[str]) -> tuple[str, str]:
    """The deepest frame above the exception line: normalized form, and the full path."""
    for line in reversed(lines):
        matched = _FRAME.match(line)
        if matched is None:
            continue
        path, func = matched.group(1), matched.group(2).strip()
        return f"{_tail(path)}:{func}", path
    return "unknown", ""


def _tail(path: str) -> str:
    """A path's last two segments, which is what survives into the key."""
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    return "/".join(parts[-2:]) if parts else path


def error_class(message: str | None, traceback: str | None) -> str:
    """The class key for one errored sample."""
    parsed = parse_error(message, traceback)
    if parsed is None:
        return UNCLASSED
    return f"error:{parsed.type}@{parsed.frame}"


def task_error_class(message: str | None, traceback: str | None) -> str:
    """The class key for a task whose log finished `status="error"`.

    The header carries the halting error whole — message and traceback — so the scorer-threw case classes on the scorer's exception with no new read.
    """
    parsed = parse_error(message, traceback)
    if parsed is None:
        return "task:error"
    return f"task:error:{parsed.type}@{parsed.frame}"


def no_log_class(tail: str) -> str:
    """The class key for a worker that departed leaving no log behind.

    The worker's output tail is the only evidence there is; a definition that raises on import prints exactly one standard traceback there. A tail with no traceback — a frontend that printed an error and exited, or nothing at all — is the bare bucket, and the tail still travels as evidence.
    """
    parsed = parse_error(None, tail)
    if parsed is None:
        return "task:no-log"
    return f"task:no-log-exit:{parsed.type}@{parsed.frame}"


VANISHED = "task:vanished"
"""A started log whose worker is gone. The log's contents do not discriminate the cause — an OOM mid-run and a lost host read identically — so the class does not pretend to (scheduling.md §5.1)."""

OPERATOR_LIMIT = "limit:operator"
"""One run-wide class for operator-terminated samples, deliberately: upstream's taxonomy has exactly one operator limit type, every instance is the same kind of act, and `limit_reason` is free text the key must not touch — it rides in evidence (workflow.md §14).

Detected and journalled like every class, and **surfaced only at adjudication**: an operator kill is somebody's own deliberate act, so its window asks no inline question — it waits in the fold for the signoff conversation, where the residue it left in the data gets its ruling (`_tend.items._anomalies`).
"""


def zero_class(name: str, identifier: str) -> str:
    """The class key for a task whose every score is zero.

    Per task, because the finding is about one task's results; a sweep-wide grader break is many classes and one proposal, which is the grouping layer working as designed.
    """
    return f"score:zero:{_readable(name) or 'task'}:{digest8(identifier)}"


def scan_class(scanner: str, label: str | None, *, task: str, identifier: str) -> str:
    """The class key for samples one scanner flagged, in one task.

    **Scanner, label, and the task.** Per task like `zero_class`, because a scan finding is decided when its task lands: the agent reads that task's flagged transcripts against that task's scores and puts the task's findings to the operator as one conversation, and a ruling given there must touch no other task's samples. An earlier design keyed on scanner and label alone, on the argument that a model which games a grader games it everywhere — which is true of the phenomenon and false of the decision, since the decision is made task by task as each one finishes. What that design shared across tasks is carried instead as precedent: a ruling on the same scanner and label in another task is printed beside the window (`scan_family`).

    The known cost is the mirror of this module's own doctrine: `error:{type}@{frame}` identifies a *mechanism*, where a label names a *category*, so one class can hold two findings that are not the same thing. The agent's investigation is what separates them, which is why the runbook has it read every flagged transcript rather than the exemplar.

    Args:
        scanner: The scanner's merge name, as the run committed it.
        label: The result's own label — `scoring_integrity`'s issue category — or `None` for a scanner that sets none.
        task: The task's name, for a key a person can read.
        identifier: The task identifier, which the digest separates two models of one task by.

    Returns:
        `scan:{scanner}:{label}:{task}:{digest}`, or `scan:{scanner}:{task}:{digest}` for a scanner that sets no label.
    """
    readable = _segment(scanner) or "scanner"
    tail = _segment(label or "")
    suffix = f"{_readable(task) or 'task'}:{digest8(identifier)}"
    return f"scan:{readable}:{tail}:{suffix}" if tail else f"scan:{readable}:{suffix}"


def scan_family(class_key: str) -> str:
    """A scan class key without its task: the scanner and label every task's window of one finding shares.

    What precedent is looked up across tasks by, so the second task to land with reward hacking sees what the first was ruled. Any other key, or a scan key too short to carry a task, is its own family.
    """
    segments = class_key.split(":")
    if kind_of(class_key) != "scan" or len(segments) < 4:
        return class_key
    return ":".join(segments[:-2])


def scan_task(class_key: str) -> str:
    """The task segment of a scan class key, for a precedent line that says which task a ruling was on."""
    segments = class_key.split(":")
    if kind_of(class_key) != "scan" or len(segments) < 4:
        return ""
    return segments[-2]


def scan_error_class(scanner: str, traceback: str | None) -> str:
    """The class key for transcripts one scanner could not scan.

    **Scanner plus exception type, which is the sample-error doctrine applied one layer in** (scheduling.md §4.2). The population it produces is the whole distinction the design asked for and nothing has to compute it: *scanning is broken* is one class spanning five hundred transcripts, *this transcript breaks the scanner* is a class of one, and both read off the window's own evidence.

    **Keyed off the traceback and nothing else**, which is not a preference. Scout writes three error columns and only one of them can be classed: `scan_error` is `str(ex)` where this module's message fallback wants `repr(ex)`, so `parse_error` never matches it; `scan_error_type` is the literal string `"refusal"` on every error row regardless of what happened, so it carries no information at all. `scan_error_traceback` is a real `traceback.format_exc()` and parses exactly as a sample's does.

    Args:
        scanner: The scanner's merge name, as the run committed it.
        traceback: The row's `scan_error_traceback`, or `None` where the column is empty.

    Returns:
        `scanerror:{scanner}:{type}@{frame}`, or `scanerror:{scanner}` where nothing parses.
    """
    readable = _segment(scanner) or "scanner"
    parsed = parse_error(None, traceback)
    if parsed is None:
        return f"scanerror:{readable}"
    return f"scanerror:{readable}:{parsed.type}@{parsed.frame}"


def _readable(value: str) -> str:
    """A key segment reduced to what is safe to print and split on."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def _segment(value: str) -> str:
    """A key segment that is readable where it can be and identity-preserving always.

    **Sanitizing alone merges what it should split.** `unsafe output` and `unsafe-output` are two labels a scanner can plausibly emit and `_readable` maps both to `unsafe-output` — one ruling then settles findings the operator who made it never saw, which is the one direction this module's over-merge-is-recoverable doctrine does *not* cover: an over-merged exception class holds two call sites of one failure, where an over-merged label holds two different findings.

    So a value the sanitizer changed carries a digest of its original, and one it left alone carries nothing. The common case — every `IntegrityLabel`, every ordinary scanner name — is untouched and stays typable, and the awkward case is unambiguous rather than pretty. `zero_class` reaches this differently and needs no help: its identity rides in a `digest8` of the task identifier that is always appended.
    """
    readable = _readable(value)
    return readable if readable == value else f"{readable}-{digest8(value)}"


def kind_of(class_key: str) -> str:
    """A class key's kind — its first segment: `error`, `limit`, `task`, `score`, `scan`, or `scanerror`."""
    return class_key.partition(":")[0]


def matching_keys(keys: Sequence[str], token: str) -> list[str]:
    """The keys a token names, by the narrowest tier that names any.

    Three tiers, each consulted only where the one before names nothing: the token is a key; the token is a prefix of a key; the token's `:`-separated parts name successive segments of a key. In the last tier every part is tried as a whole segment before any is taken as a prefix of one, so a label that is also the start of a longer label is not ambiguous with it. `internet_egress` names `scan:scoring_integrity:internet_egress:cybench:1a2b3c4d`, `internet_egress:cybench` picks that one out from the same finding on another task, and `TimeoutError` names `error:TimeoutError@openai/_client.py:post`.

    Args:
        keys: The keys a token may name.
        token: What somebody typed.

    Returns:
        Every key the narrowest tier names — one where the token is unambiguous, several where it is not, none where it names nothing.
    """
    if not token:
        return []
    if exact := [key for key in keys if key == token]:
        return exact
    if prefixed := [key for key in keys if key.startswith(token)]:
        return prefixed
    parts = [part for part in token.split(":") if part]
    if not parts:
        return []
    whole = [key for key in keys if _subsequence(key.split(":"), parts, str.__eq__)]
    if whole:
        return whole
    return [key for key in keys if _subsequence(key.split(":"), parts, str.startswith)]


def _subsequence(
    segments: Sequence[str], parts: Sequence[str], match: Callable[[str, str], bool]
) -> bool:
    """Whether `parts` name segments of `segments` in order, under `match`, skipping any."""
    remaining = iter(segments)
    return all(any(match(segment, part) for segment in remaining) for part in parts)


def short_token(
    keys: Sequence[str], key: str, *, reserved: Collection[str] = ()
) -> str:
    """The shortest token `matching_keys` resolves to exactly `key` among `keys`.

    A scan class answers to its label, then `label:task`; an error class to its exception type; a score class to `zero:task`; a task class to its two leading segments — and any of them to the whole key when nothing shorter is unique. `reserved` are the names a verb reads before it reads class tokens (task display keys), which a token must not be the start of.
    """
    segments = key.split(":")
    kind = segments[0]
    candidates: list[str] = []
    if kind == "scan" and len(segments) == 5:
        candidates += [segments[2], f"{segments[2]}:{segments[3]}"]
    elif kind == "scan" and len(segments) == 4:
        candidates += [segments[1], f"{segments[1]}:{segments[2]}"]
    elif "@" in key:
        typed = next(segment for segment in segments if "@" in segment)
        candidates.append(typed.partition("@")[0])
    elif kind == "score" and len(segments) >= 3:
        candidates.append(f"zero:{segments[2]}")
    elif len(segments) >= 2:
        candidates.append(":".join(segments[:2]))
    for candidate in candidates:
        if any(name.startswith(candidate) for name in reserved):
            continue
        if matching_keys(keys, candidate) == [key]:
            return candidate
    return key


def digest8(value: str) -> str:
    """Eight hex of a hash, for keys built over text nobody should read back."""
    return sha256(value.encode("utf-8")).hexdigest()[:8]


def cancelled(message: str | None) -> bool:
    """Whether an error message is a cancellation repr — teardown, not an instance."""
    return is_cancellation_message(message)


_STORAGE_PATHS = (
    "/fsspec/",
    "/s3fs/",
    "/botocore/",
    "/aiobotocore/",
    "/boto3/",
    "/gcsfs/",
    "/inspect_ai/log/",
)
"""Raise sites that are substrate whatever the exception's type: the storage stack, and inspect's own log-writing path."""

_SUBSTRATE_TYPES = frozenset(
    {
        "OSError",
        "IOError",
        "PermissionError",
        "NoCredentialsError",
        "CredentialRetrievalError",
        "ExpiredTokenError",
        "ClientError",
        "EndpointConnectionError",
        "S3Error",
    }
)
"""Concrete exception names that mean the substrate, matched on the final dotted segment exactly — which is what keeps `FileNotFoundError`, an `OSError` subclass that is usually a dataset bug, out."""

_SUBSTRATE_MARKERS = (
    "No space left on device",
    "ENOSPC",
    "Read-only file system",
    "EROFS",
    "EDQUOT",
    "ExpiredToken",
    "AccessDenied",
    "InvalidAccessKeyId",
)
"""Message and errno fragments that mean the substrate. Message text is legitimate *here* because the flag is classification metadata, not identity — it never splits or merges a class, it colours one."""


def substrate(parsed: ParsedError | None, message: str | None) -> bool:
    """Whether a failure is the machinery under the run rather than the run.

    Eager by design, because the asymmetry is real (execution.md §9.1): a false negative re-runs into a broken substrate and burns the work twice; a false positive delays a re-run until an operator confirms — cheap, visible, recoverable. A substrate class gets no re-run proposal; an operator ruling on it *is* the by-hand verification the doctrine asks for.
    """
    if parsed is not None:
        if any(marker in parsed.path for marker in _STORAGE_PATHS):
            return True
        if parsed.type.rsplit(".", 1)[-1] in _SUBSTRATE_TYPES:
            return True
    return any(marker in message for marker in _SUBSTRATE_MARKERS) if message else False


__all__ = [
    "MESSAGE_CAP",
    "OPERATOR_LIMIT",
    "UNCLASSED",
    "VANISHED",
    "ParsedError",
    "cancelled",
    "digest8",
    "error_class",
    "kind_of",
    "matching_keys",
    "no_log_class",
    "parse_error",
    "scan_class",
    "scan_family",
    "scan_task",
    "short_token",
    "scan_error_class",
    "substrate",
    "task_error_class",
    "zero_class",
]
