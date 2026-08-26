"""`_steward.md` — everything this human has told Steward, structured and not.

One file with two regions. **The YAML front matter is what Steward executes at 3am with nobody watching; the prose below it is what an agent applies when it arrives.** They live together because the line between them is a fact about Steward's current capability rather than about the author's intent, and it moves as Steward improves — two files would make the reader's mental model track the implementation. Adjacency is the point: a human writes one sentence about concurrency and the executable half sits directly above the reasoning, where neither can drift from the other.

**The rule that makes the file safe is that it may express only what the definition cannot** (workflow.md, *A config file may not say anything the definition can*). Things affecting Steward, never things affecting Inspect. `max_workers` qualifies because the fan-out into processes is Steward's invention and no `eval_set()` argument reaches it; `max_samples` does not, and is refused by name. Two mechanisms enforce it, and both fail loudly on the first read, because the failure mode being guarded against is a key someone adds in good faith and never learns was ignored:

- **Keys the definition owns are refused by name**, with a message saying where they belong.
- **Everything else unrecognised is rejected outright.** Same posture as a selection document, for the same reason: this is input, not history.

**Steward parses the front matter and never reads the body.** The agent opens the file itself, so nothing here has to understand markdown — which also means no prose, however malformed, can break a tend.

**Every value is typed, and validated strictly.** That is what answers YAML's coercion hazards, which workflow.md §5.3 could dismiss for the journal on the grounds that Steward wrote it and here cannot, because a human does. Typing alone is not enough and it is worth saying why: pydantic's default is coercive and YAML's is too, so the two compose into the hazard rather than cancelling it — `max_workers: yes` arrives as `True` and validates as `1`, throttling a fleet to a single worker with nothing reported. `strict=True` is what turns that into a refusal, and the error names the value that arrived so the author can see what YAML did to it.

**Parsing is strict, and degrading is the caller's.** A malformed file raises here, always — this module has no way to know whether the caller has anything better to fall back on. A tend does: the settings in force are recorded in every `observation`, so it can carry on with the last known good ones and say so, which is the right behaviour for a file a human may edit at 10pm while a fleet is up. A command with no such history still refuses (`_tend.turn`).

That §5.3 rejected markdown-with-front-matter for `journal.jsonl` is not in tension with this. Its argument was that block-delimited formats fail *globally* — one mistyped `---` swallows the remainder of a file — which is disqualifying for an append-only log of thousands of machine-written entries. This is a single human-authored block read at startup, with exactly one fence to get wrong and a loud error when it is.
"""

from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .._schedule import DEFAULT_MAX_WORKERS, DEFAULT_STALL_AFTER, Pool
from .._util.duration import parse_duration

DEFAULT_TEND_INTERVAL = 600
"""Seconds between scheduled tends where nobody said otherwise.

Ten minutes, because the cost of a turn is bounded by what it reads and the cost of *missing* one is a fleet sitting idle for the whole interval. Short enough that an empty slot is refilled while somebody is still awake to care; long enough that a settled directory of two thousand logs is not re-read every minute.
"""

FENCE = "---"
"""Opens and closes the front matter. Recognised only at the very start of the file and then at column zero, so a horizontal rule in the prose is prose."""

_DEFINITION = "your definition's `eval_set()` call"

REFUSED: dict[str, str] = {
    "log_dir": _DEFINITION,
    "model": _DEFINITION,
    "models": _DEFINITION,
    "tasks": _DEFINITION,
    "epochs": _DEFINITION,
    "limit": _DEFINITION,
    "solver": _DEFINITION,
    "scorer": _DEFINITION,
    "sandbox": _DEFINITION,
    "token_limit": _DEFINITION,
    "time_limit": _DEFINITION,
    "message_limit": _DEFINITION,
    "fail_on_error": _DEFINITION,
    "retry_on_error": _DEFINITION,
    "max_samples": _DEFINITION,
    "notify": "the INSPECT_EVAL_NOTIFICATION environment variable",
    "notification": "the INSPECT_EVAL_NOTIFICATION environment variable",
    "store": "the INSPECT_STEWARD_STORE environment variable",
}
"""Keys that belong somewhere else, and where.

Everything above the notification rows is the definition's, and configuration.md establishes the definition as the single source of truth for what an eval set *is* — a second file beside it saying otherwise is the drift this whole rule exists to prevent. `max_samples` is here rather than in the model because the definition can express it and two other layers move it at runtime; Steward's own `--max-samples` remains available for one run.

The last three are reference-only *by design* upstream, so that credentials stay out of source, shell history, process listings, and eval logs. Accepting them here would break a discipline Steward otherwise inherits for free. Note the distinction this draws: a notification **URL** is refused; notification **policy** — kinds, cadence, quiet hours — is Steward's alone and becomes a key when there is something to read it.
"""


class DirectivesError(Exception):
    """`_steward.md` could not be read as directives.

    Raised rather than reported, because there is no useful way to proceed: running on defaults would silently discard an operator's instruction, which is the one outcome worse than stopping. Every message names the file and, where the key belongs somewhere else, says where.
    """


class Directives(BaseModel):
    """What the front matter said.

    Carries the structured half only. The prose is the agent's to read, and nothing here parses it.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    """`strict` is load-bearing rather than tidy, and removing it reintroduces a silent bug.

    Pydantic's default is coercive, and YAML's is too, so the two compose into exactly the hazard this file is supposed to be safe from: `max_workers: yes` becomes `True` becomes `1`, and a workspace that meant to say something ends up throttled to a single worker with nothing reported. `"8"` and `8.0` land the same way. Strict validation is what makes *every value is typed* an actual guarantee instead of a description of the annotations.
    """

    max_workers: int | None = Field(default=None, gt=0)
    """Ceiling on concurrent workers, or `None` where the workspace expressed no preference.

    A standing property of the host and the workspace rather than a setpoint — "do not exceed 8 workers here" — which is what makes it the operator's envelope rather than something a tuning loop moves (workflow.md, *The envelope is policy; the tuning is the agent's job*).
    """

    stall_after: int | None = Field(default=None, gt=0)
    """Consecutive fruitless attempts before a task stops being respawned, or `None` for the default.

    Passes the same test `max_workers` does: respawning a task is Steward's invention, and no `eval_set()` argument reaches it, so there is nothing here for a definition to contradict. How much patience a project's failures deserve is genuinely a standing property of the project — a flaky sandbox fleet earns more of it than a deterministic scorer bug does (`_schedule.reconcile._stalled`).
    """

    tend_interval: int | None = Field(default=None, gt=0)
    """Seconds between scheduled tends, or `None` for the default.

    Written with a unit — `tend_interval: 10m` — and stored as seconds. Passes the same test the two above do: how often to converge is a property of the host and the workspace, and no `eval_set()` argument reaches it.

    A standing preference rather than what is currently installed. `steward timer arm` reads it, but a timer armed at one interval and a file later edited to another disagree until somebody re-arms, which is a `timer_drift` item rather than something a tend quietly fixes — reaching into a user's crontab unprompted is not a mechanical act.
    """

    @field_validator("tend_interval", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        """A duration is written with its unit, and a bare number is refused.

        The one place strictness is not enough on its own. `tend_interval: 10` is a perfectly good integer and means ten minutes to whoever typed it and ten seconds to whoever wrote the parser, so it is rejected rather than guessed at — the same posture as every other key here, applied to a hazard typing cannot see.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                f"must be written with a unit, like '10m' — {value!r} could mean "
                f"seconds, minutes, or hours, and Steward will not guess"
            )
        # DurationError is a ValueError, so a bad unit arrives as a field error
        # naming the value, like every other refusal in this file
        return parse_duration(value)


def read_directives(path: Path) -> Directives:
    """Read a workspace's directives.

    Args:
        path: `_steward.md`. Need not exist.

    Returns:
        The front matter's settings. All defaults when the file is absent, has no front matter, or has an empty one — an absent file is a workspace that expressed no preferences, not an error.

    Raises:
        DirectivesError: The front matter is unterminated, is not valid YAML, is not a mapping, or names a key that belongs elsewhere.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Directives()
    except OSError as ex:
        raise DirectivesError(f"{path.name} could not be read: {ex}") from ex
    except UnicodeDecodeError as ex:
        # not an OSError, and an editor that saved as latin-1 is the ordinary
        # way to get one -- without this it is a traceback rather than a
        # message naming the file
        raise DirectivesError(f"{path.name} is not valid UTF-8: {ex}") from ex

    front = _front_matter(text, name=path.name)
    if front is None:
        return Directives()

    try:
        loaded: Any = yaml.safe_load(front)
    except yaml.YAMLError as ex:
        raise DirectivesError(
            f"the front matter in {path.name} is not valid YAML: {ex}"
        ) from ex

    if loaded is None:
        return Directives()
    if not isinstance(loaded, dict):
        raise DirectivesError(
            f"the front matter in {path.name} must be a mapping of settings, "
            f"not {type(loaded).__name__}"
        )

    settings = cast(dict[str, Any], loaded)
    _refuse(settings, name=path.name)

    try:
        return Directives.model_validate(settings)
    except ValidationError as ex:
        raise DirectivesError(f"{path.name} is not valid: {_explain(ex)}") from ex


def resolve_pool(
    directives: Directives,
    *,
    max_workers: int | None = None,
    max_samples: int | None = None,
) -> Pool:
    """Resolve what the operator asked of the worker pool.

    Two chains, and the difference between them is the rule made visible:

    | | |
    |---|---|
    | `max_workers` | the CLI, then `_steward.md`, then the default |
    | `stall_after` | `_steward.md`, then the default — there is no flag, because patience is a standing property rather than something to retype each turn |
    | `max_samples` | the CLI, then **the definition**, then the default — the file is not a source |

    The last chain continues inside `resolve_max_samples`, which is why `None` is passed straight through rather than filled in here: *no preference* yields to whatever the definition asked for, and a number is an instruction that does not.

    Args:
        directives: What the workspace's front matter said.
        max_workers: Ceiling from the command line, or `None`.
        max_samples: Sample concurrency from the command line, or `None`.

    Returns:
        What the operator asked for, for `reconcile`.
    """
    # written lowest-precedence first, so the chain reads in the order it resolves
    ceiling = DEFAULT_MAX_WORKERS
    if directives.max_workers is not None:
        ceiling = directives.max_workers
    if max_workers is not None:
        ceiling = max_workers

    return Pool(
        max_workers=ceiling,
        max_samples=max_samples,
        stall_after=(
            directives.stall_after
            if directives.stall_after is not None
            else DEFAULT_STALL_AFTER
        ),
    )


def resolve_interval(directives: Directives, *, interval: str | None = None) -> int:
    """Resolve how often this workspace should tend.

    The `max_workers` chain, one key over: the command line, then `_steward.md`, then the default. An interval is a standing property of the host, so the file is a real source for it — unlike `max_samples`, whose source is the definition.

    Args:
        directives: What the workspace's front matter said.
        interval: A duration from the command line, e.g. `10m`, or `None`.

    Returns:
        Seconds between tends.

    Raises:
        DurationError: `interval` is not a duration.
    """
    if interval is not None:
        return parse_duration(interval)
    if directives.tend_interval is not None:
        return directives.tend_interval
    return DEFAULT_TEND_INTERVAL


def _front_matter(text: str, *, name: str) -> str | None:
    """The YAML between the fences, or `None` where there is no front matter.

    The opening fence must be the file's first line, so a `policy.md` of pure prose renamed to `_steward.md` keeps working and a horizontal rule further down stays a horizontal rule. An *unterminated* fence is an error rather than a file read to the end: that is the one failure §5.3 names, and catching it is what makes a single block safe where an append-only log of them would not be.
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != FENCE:
        return None

    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip() == FENCE:
            return "\n".join(lines[1:index])

    raise DirectivesError(
        f"the front matter in {name} opens with `{FENCE}` and is never closed — "
        f"add a `{FENCE}` line where the settings end"
    )


def _refuse(settings: dict[str, Any], *, name: str) -> None:
    """Reject keys that belong elsewhere, before anything else looks at them.

    Ahead of validation deliberately: `extra="forbid"` would call these unknown, which is both untrue and unhelpful next to a message that names the destination.
    """
    for key, destination in REFUSED.items():
        if key in settings:
            raise DirectivesError(
                f"`{key}` does not belong in {name} — set it in {destination}"
            )


def _explain(error: ValidationError) -> str:
    """A validation failure as one clause per offending key.

    Two departures from pydantic's own wording, both because the default describes the mechanism rather than the mistake. *Extra inputs are not permitted* becomes a sentence about the key. And a **type** failure names what arrived, which is the whole value of validating strictly: someone who wrote `max_workers: yes` needs to see `True` to understand that YAML rewrote it, and *should be a valid integer* on its own does not tell them.
    """
    clauses: list[str] = []
    for item in error.errors():
        key = ".".join(str(part) for part in item["loc"])
        message = item["msg"].replace("Input should be", "should be", 1)
        if item["type"] == "extra_forbidden":
            clauses.append(f"`{key}` is not a setting Steward knows")
        elif not key:
            clauses.append(item["msg"])
        elif item["type"].endswith("_type"):
            clauses.append(f"`{key}` {message}, not {item['input']!r}")
        else:
            clauses.append(f"`{key}` {message}")
    return "; ".join(clauses)
