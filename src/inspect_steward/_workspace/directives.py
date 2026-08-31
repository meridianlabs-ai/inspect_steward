"""`_steward.yaml` — everything this human has told Steward, structured and not.

One file with two regions. **The settings are what Steward executes at 3am with nobody watching; `policies` is what an agent applies when it arrives.** They live together because the line between them is a fact about Steward's current capability rather than about the author's intent, and it moves as Steward improves — two files would make the reader's mental model track the implementation. Adjacency is the point: a human writes one sentence about concurrency and the executable half sits directly beside the reasoning, where neither can drift from the other. The seam being a key rather than a delimiter is what lets a rule graduate from prose to setting by moving out of `policies` and up the file, with nothing reformatted.

**The rule that makes the file safe is that it may express only what the definition cannot** (workflow.md, *A config file may not say anything the definition can*). Things affecting Steward, never things affecting Inspect. The sharpest form of the rule, and the one the current key set obeys with no exceptions to explain: **inspect's words go in the definition; this file holds only words `eval_set()` does not know.** `max_workers` qualifies because fanning an eval set across processes is Steward's invention and no `eval_set()` argument reaches it; `max_samples` and `max_tasks` do not, and are refused by name.

That rule is stricter than the test it replaced. `max_tasks` used to live here, justified by *does a definition's value reach the runtime* — it does not, because every selection document overrides it with that worker's own batch size, so the key contradicted nothing. The test was sound and the key was still confusing: `eval_set()` knows the word, so somebody writing it there watched it do nothing while a same-named key sat in the policy file. Fleet width now resolves from the definition (`_schedule.resolve_max_tasks`), which cost one upstream field and bought a rule with no exceptions (execution.md, item 17).

`samples_ramp` is the near miss worth naming, since it governs sample concurrency and sits next to a refused `max_samples`. It stays because `eval_set()` has no such word: a *range to discover a setpoint within* is not something a definition can express, and the moment a definition does express a setpoint the key goes inert rather than contradicting it. Two mechanisms enforce it, and both fail loudly on the first read, because the failure mode being guarded against is a key someone adds in good faith and never learns was ignored:

- **Keys the definition owns are refused by name**, with a message saying where they belong.
- **Everything else unrecognised is rejected outright.** Same posture as a selection document, for the same reason: this is input, not history.

**Steward parses `policies` and never interprets it.** The value is carried, not read: an agent is the audience, and Steward's only interest is being able to report what is in force. It is a string or a list of them, because a project with three rules wants three list entries and a project with a page of reasoning wants a block scalar, and neither should have to pretend to be the other.

The cost of the format is here rather than hidden. When the prose sat below a fence, no prose however malformed could break a tend, because the parser stopped at the closing delimiter. Now it is a YAML value, so a mis-indented block scalar is a parse failure for the whole file. That is a real loss and it is survivable for a reason that already existed: parsing raises, and a tend degrades. See below.

**Every value is typed, and validated strictly.** That is what answers YAML's coercion hazards, which workflow.md §5.3 could dismiss for the journal on the grounds that Steward wrote it and here cannot, because a human does. Typing alone is not enough and it is worth saying why: pydantic's default is coercive and YAML's is too, so the two compose into the hazard rather than cancelling it — `max_workers: yes` arrives as `True` and validates as `1`, throttling a fleet to a single worker with nothing reported. `strict=True` is what turns that into a refusal, and the error names the value that arrived so the author can see what YAML did to it.

**Parsing is strict, and degrading is the caller's.** A malformed file raises here, always — this module has no way to know whether the caller has anything better to fall back on. A tend does: the settings in force are recorded in every `observation`, so it can carry on with the last known good ones and say so, which is the right behaviour for a file a human may edit at 10pm while a fleet is up. A command with no such history still refuses (`_tend.turn`).

That §5.3 rejected markdown-with-front-matter for `journal.jsonl` is not in tension with this, and is in fact the argument that eventually removed the fence here too. Its case was that block-delimited formats fail *globally* — one mistyped `---` swallows the remainder of a file — which is disqualifying for an append-only log of thousands of machine-written entries and merely unhelpful for one human-authored file read at startup. What settled it is that the fence was never buying anything: the prose below it was already a value Steward carried rather than a document it parsed, so making that explicit costs one failure mode and removes a delimiter nobody needed to learn.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

# the overrides model is a versioned wire format, deliberately not public API
from inspect_ai._eval.eval_set_overrides import EvalSetOverrides
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .._schedule import DEFAULT_STALL_AFTER, Pool
from .._util.duration import parse_duration

PREFIX = "STEWARD_"
"""Every environment variable that changes what Steward does, and nothing else.

One namespace, so *is this Steward's?* is answerable by looking. The cost is that Steward's own internals have to stay out of it or be named in `RESERVED`.
"""

RESERVED = frozenset({"STEWARD_WORKER", "STEWARD_TASK"})
"""Variables under the prefix that are not settings.

Both are markers Steward writes into a worker's environment and reads back off the process table (`_worker.inflight`), so they are always present in exactly the processes that would otherwise refuse them as typos.
"""

DEFAULT_TEND_INTERVAL = 600
"""Seconds between scheduled tends where nobody said otherwise.

Ten minutes, because the cost of a turn is bounded by what it reads and the cost of *missing* one is a fleet sitting idle for the whole interval. Short enough that an empty slot is refilled while somebody is still awake to care; long enough that a settled directory of two thousand logs is not re-read every minute.
"""

STEWARDS: frozenset[str] = frozenset({"log_dir", "notification"})
"""Override fields that are Steward's word rather than inspect's, and so get no alias.

Two names that belong to `eval_set()` everywhere else and to Steward here. Both qualify on the same test: the answer is a property of the **deployment** rather than of the eval, so there is no scope at which somebody else decides it — and each has a Steward setting carrying all three spellings in its place.

`log_dir`, because a run's logs go where the fleet is watched from. A variable that quietly moved them would leave every tend reading an empty directory while its workers wrote somewhere else.

`notification`, because Steward's channel and the fleet's are one channel. The setting resolves a value that reaches a worker two ways at once — exported as `INSPECT_EVAL_NOTIFICATION` so `build_apprise` can read it, and applied as the `notification` override so the worker's `eval_set()` asks it to. A second, independent spelling of the same field would let those two disagree, and the shape of that disagreement is a fleet that silently never notifies.
"""

ALIASES: dict[str, str] = {
    f"{PREFIX}{field.upper()}": field
    for field in EvalSetOverrides.model_fields
    if field not in STEWARDS
}
"""Names under the prefix that are inspect's words rather than Steward's.

A scoped alias of an `INSPECT_EVAL_*` variable, resolved by `overrides.py` and not by this module — the settings here and the overrides there are two vocabularies that share one namespace, and only the namespace is common. Defined on this side of the pair because this is the module that polices the prefix, and because a name that is neither a setting nor an alias has to be refused by something that knows both.

`STEWARDS` is excluded, so `STEWARD_NOTIFICATION` reaches the settings below rather than the overrides beside them.
"""

SUPERSEDED = "_steward.md"
"""The name this file used to have, refused by name so a rename is reported rather than silently ignoring a workspace's standing rules."""

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
    "working_limit": _DEFINITION,
    "fail_on_error": _DEFINITION,
    "retry_on_error": _DEFINITION,
    "max_samples": _DEFINITION,
    # the one refusal that says more than where the key goes, because this key
    # used to live here and deleting it is not the same as moving it: Steward
    # reads an unset fleet width as *everything at once*
    "max_tasks": (
        f"{_DEFINITION}, which is now where fleet width comes from — and move it "
        f"rather than deleting it, since unset means every task at once"
    ),
    "notify": "`notification`, which is what it is called here",
    "store": "`log_store`, which is what it is now called",
}
"""Keys that belong somewhere else, and where.

Everything above the notification rows is the definition's, and configuration.md establishes the definition as the single source of truth for what an eval set *is* — a second file beside it saying otherwise is the drift this whole rule exists to prevent. `max_samples` is here rather than in the model because the definition can express it and two other layers move it at runtime.

`notify` and `store` are renames rather than relocations: both keys used to exist here under those names and both still exist under others, so pointing at the new name is the whole message.

**The notification channel used to be refused here and is now `notification` below**, which reverses the position this table took. The old rule was inherited from upstream, where notification config is reference-only so that credentials stay out of source, shell history, process listings, and eval logs — and the half of it Steward keeps is that nothing ever *prints* or *propagates* the value (`status` renders policies alone; `_workspace.sync` omits this key from the copy it writes beside the logs). What changed is the judgement about the file itself: an unattended run that cannot reach a person is the failure the whole design exists to prevent, and making the channel sayable in the one place that survives to 02:00 is worth more than the discipline of refusing it. A workspace that would rather not commit one writes `.env` or the variable, which is what the template recommends.
"""


class DirectivesError(Exception):
    """`_steward.yaml` could not be read as directives.

    Raised rather than reported, because there is no useful way to proceed: running on defaults would silently discard an operator's instruction, which is the one outcome worse than stopping. Every message names the file and, where the key belongs somewhere else, says where.
    """


class Directives(BaseModel):
    """What `_steward.yaml` said.

    Both halves, since `policies` is a key like any other now — but only one of them means anything to Steward. The settings are executed; the prose is carried so that a command can report what is in force, and interpreted by an agent rather than here.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    """`strict` is load-bearing rather than tidy, and removing it reintroduces a silent bug.

    Pydantic's default is coercive, and YAML's is too, so the two compose into exactly the hazard this file is supposed to be safe from: `max_workers: yes` becomes `True` becomes `1`, and a workspace that meant to say something ends up throttled to a single worker with nothing reported. `"8"` and `8.0` land the same way. Strict validation is what makes *every value is typed* an actual guarantee instead of a description of the annotations.
    """

    max_workers: int | None = Field(default=None, gt=0)
    """How many worker processes a run uses, or `None` for a process per task.

    A standing property of the host and the workspace rather than a setpoint — "do not run more than 8 processes here" — which is what makes it the operator's envelope rather than something a tuning loop moves (workflow.md, *The envelope is policy; the tuning is the agent's job*).

    Fewer processes than tasks means packing several tasks into each, which buys back the per-process startup a frontend charges and costs crash isolation. It does not change how much runs at once: that is `max_tasks`.
    """

    stall_after: int | None = Field(default=None, gt=0)
    """Consecutive fruitless attempts before a task stops being respawned, or `None` for the default.

    Passes the same test `max_workers` does: respawning a task is Steward's invention, and no `eval_set()` argument reaches it, so there is nothing here for a definition to contradict. How much patience a project's failures deserve is genuinely a standing property of the project — a flaky sandbox fleet earns more of it than a deterministic scorer bug does (`_schedule.reconcile._stalled`).
    """

    stuck_after: int | None = Field(default=None, gt=0)
    """Seconds a running sample may go without activity before it is reported stuck, or `None` for the default.

    Written with a unit — `stuck_after: 5h` — and stored as seconds, exactly like `tend_interval`. Named to rhyme with `stall_after`, because the two are siblings: a *stalled* task's worker keeps dying, a *stuck* sample's worker is perfectly healthy and one call inside it never returns.

    **A reporting threshold, never a limit.** Nothing is cancelled when it trips — a `stuck` item appears, carrying the `inspect ctl` line that would act. The enforced budget is the definition's `working_limit`, which is refused here by name; this key decides when a person hears about a sample the limit has not caught. Passes the admission test the same way `stall_after` does: watching a fleet for silence is Steward's invention, and no `eval_set()` argument reaches it. Default: `DEFAULT_STUCK_AFTER` (`_worker.live`), five hours.
    """

    stuck_cancel: bool | list[str] | None = Field(default=None)
    """Which stuck pending tool calls the agent may cancel without asking, or unset for none.

    `true` admits any tool function; a list admits only those named (`stuck_cancel: [bash]`); unset or `false` admits nothing — the default, because cancelling a call loses whatever it was mid-way through. What it moves is the `stuck` item's owner: a task whose every stuck sample is a pending call this key admits is the agent's to act on (rung 1 of the ladder, execution.md §7.5), and everything else stays a person's.

    `true` is meaningful here where `notification: true` is refused, because *any tool call* is a real universal where *somewhere* is not an address.
    """

    @field_validator("stuck_cancel", mode="before")
    @classmethod
    def _stuck_cancel(cls, value: object) -> object:
        """A boolean or a list of tool function names, refused with its meaning otherwise.

        `mode="before"` for the list case: strict validation would report a list holding an integer as a type error against the whole field, when the author needs told which entry is wrong. An empty list admits nothing, which is what unset already says.
        """
        if not isinstance(value, list):
            return value
        entries = cast(list[object], value)
        for index, entry in enumerate(entries):
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    f"should be `true`, `false`, or a list of tool function "
                    f"names — entry {index + 1} is not a name: {entry!r}"
                )
        return entries or None

    preauthorized: dict[str, str] | bool | None = Field(default=None)
    """Rulings granted in advance: anomaly-class patterns mapped to the disposition each is authorized to receive, `false` to decline every standing grant, or `None` for none.

    Keys are `fnmatch` patterns over class keys (`error:*APITimeoutError*`), values one of the doctrinal four — `rerun`, `exclude`, `zero`, `score`. A tend that finds an open window matching a pattern journals an ordinary `ruling` with `by: policy` naming the pattern, then applies it like any other — the machine-readable half of the "rulings granted in advance" bullet in `policies`. First matching pattern wins, in file order. Nothing is pre-authorized by default.

    `false` is the off-switch, and it exists because `None` cannot be one: an unset narrower scope defers to the file, so without a spellable *nothing* there would be no way to hold the file's grants back for one turn — `--preauthorized false`, or `STEWARD_PREAUTHORIZED=false` on a machine that must not auto-rule (the `sync: false` shape). An empty mapping normalizes to `None`, because grants nothing and expresses no preference are the same instruction from a file and dangerously different ones from a flag.

    `accept` and `dismiss` are refused by name: an accept needs an `--effect` sentence no pattern can carry, and a dismissal granted in advance is a wave-past — the one thing a standing rule must not be. Patterns are data: whether a disposition fits a class's *kind* (no `exclude` against a `task:` class) is checked at application time, since a pattern's targets are unknowable here.
    """

    @field_validator("preauthorized", mode="before")
    @classmethod
    def _preauthorized(cls, value: object) -> object:
        """Patterns to dispositions, with the two ineligible dispositions refused by name."""
        if value is None or value is False:
            return value
        if value is True:
            raise ValueError(
                "should be a mapping of class patterns to dispositions, or "
                "`false` to decline every standing grant — `true` grants "
                "nothing nameable"
            )
        if not isinstance(value, dict):
            raise ValueError(
                f"should be a mapping of class patterns to dispositions, "
                f"like `'error:*Timeout*': rerun` — not {value!r}"
            )
        entries = cast(dict[object, object], value)
        for pattern, disposition in entries.items():
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(
                    f"patterns must be class-key text, like `error:*Timeout*` — "
                    f"not {pattern!r}"
                )
            if disposition == "accept":
                raise ValueError(
                    f"`{pattern}: accept` cannot be granted in advance — accept "
                    f"carries an effect sentence the report needs, and no "
                    f"pattern can write one; rule it with `steward rule ... "
                    f"--effect`"
                )
            if disposition == "dismiss":
                raise ValueError(
                    f"`{pattern}: dismiss` cannot be granted in advance — a "
                    f"dismissal nobody looked at is a wave-past, which is what "
                    f"the anomaly queue exists to prevent"
                )
            if disposition not in ("rerun", "exclude", "zero", "score"):
                raise ValueError(
                    f"`{pattern}: {disposition!r}` is not a disposition a "
                    f"pattern can grant — one of rerun, exclude, zero, score"
                )
        return entries or None

    samples_ramp: tuple[int, int] | bool | None = Field(default=None)
    """The range the tuning loop may explore sample concurrency over, `false` to disable it, or `None` for the default range.

    Written as a two-element list — `samples_ramp: [40, 300]` — or `false`. The default, with the key absent, is a ramp over `DEFAULT_SAMPLES_RAMP`; the floor is where every task starts, and the ceiling is how far tend may climb it while pushback stays absent (scheduling.md, *Growing `max_samples`*).

    Passes the admission test despite living next to a refused `max_samples`, because it never contradicts a definition: an explicit `max_samples` anywhere — the CLI or the definition — pins the setpoint and switches this policy off entirely, so the key governs only Steward's own exploration. An author who wants a custom start *and* a ramp expresses the start as the floor.
    """

    @field_validator("samples_ramp", mode="before")
    @classmethod
    def _ramp(cls, value: object) -> object:
        """A range is two ordered positive integers, and anything else is refused with its meaning.

        `mode="before"` because YAML can only produce a list and strict validation will not coerce one into a tuple — and because the refusals here can say more than a type error can: `true` is meaningless where `false` is not, and a one-element or inverted range is a mistake worth naming.
        """
        if value is None or value is False:
            return value
        if value is True:
            raise ValueError(
                "should be a range like [40, 300], or `false` to disable ramping — "
                "`true` says nothing about how far"
            )
        if isinstance(value, list):
            entries = cast(list[object], value)
            if (
                len(entries) == 2
                and all(
                    isinstance(entry, int) and not isinstance(entry, bool)
                    for entry in entries
                )
                and 0 < cast(int, entries[0]) <= cast(int, entries[1])
            ):
                return (entries[0], entries[1])
        raise ValueError(
            f"should be a range of two ordered positive integers, like [40, 300], "
            f"or `false` — not {value!r}"
        )

    tend_interval: int | None = Field(default=None, gt=0)
    """Seconds between scheduled tends, or `None` for the default.

    Written with a unit — `tend_interval: 10m` — and stored as seconds. Passes the same test the two above do: how often to converge is a property of the host and the workspace, and no `eval_set()` argument reaches it.

    A standing preference rather than what is currently installed. `steward timer arm` reads it, but a timer armed at one interval and a file later edited to another disagree until somebody re-arms, which is a `timer_drift` item rather than something a tend quietly fixes — reaching into a user's crontab unprompted is not a mechanical act.
    """

    @field_validator("tend_interval", "stuck_after", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        """A duration is written with its unit, and a bare number is refused.

        The one place strictness is not enough on its own. `tend_interval: 10` is a perfectly good integer and means ten minutes to whoever typed it and ten seconds to whoever wrote the parser, so it is rejected rather than guessed at — the same posture as every other key here, applied to a hazard typing cannot see. `stuck_after` shares it: `5` could mean seconds or hours, and either misreading silences or floods the one surface the key feeds.
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

    notification: str | bool | None = Field(default=None)
    """Where Steward posts what it cannot decide, `false` for nowhere, or `None` for no preference.

    An Apprise target: one URL, several separated by commas, or a path to an Apprise config file — whatever `INSPECT_EVAL_NOTIFICATION` itself accepts, so there is one format to learn rather than one per spelling.

    **The channel is a property of the deployment, which is what admits it** — `STEWARDS` states the test, and the same test excludes this field from the overrides beside it. One value serves two consumers: Steward posts to it, and it reaches every worker as `INSPECT_EVAL_NOTIFICATION` plus the `notification` override that makes a worker's `eval_set()` read it.

    **A URL here is committed, and that is a real cost accepted rather than overlooked.** `_steward.yaml` is tracked; `.env` and the variable are not, and the template says so. What the design keeps from the reference-only discipline it is departing from is that the value never *travels*: `status` renders policies alone, and `_workspace.sync` replaces this key before writing the copy that lands beside the logs, which is the one path that would otherwise put a token in an object store.

    **`false` silences Steward and not the fleet.** A worker's notifications are blocking human-in-the-loop moments — `ask_user()`, the human approver — and the opposite semantics on the same pipe (workflow.md §11.4). Silencing those would hang a sample with nobody told, which is a worse failure than the noise being declined.
    """

    @field_validator("notification", mode="before")
    @classmethod
    def _notification(cls, value: object) -> object:
        """A target, or `false`. Shaped like its siblings and refused the same way.

        `true` is refused where inspect *accepts* it, which is the one place the two vocabularies deliberately differ: to inspect it means *read the variable*, and here the variable is already one of this key's own three spellings, so it would say nothing about where.
        """
        if value is True:
            raise ValueError(
                "should be an Apprise URL, a config file, or `false` to post "
                "nowhere — `true` says nothing about where"
            )
        if isinstance(value, str) and not value.strip():
            raise ValueError(
                "should be an Apprise URL, a config file, or `false` — not an "
                "empty value"
            )
        if value == "none":
            raise ValueError(
                "is `false` now, since `none` reads as a target called none"
            )
        return value

    log_root: str | bool | None = Field(default=None)
    """The root this machine keeps eval logs under, `false` for none, or `None` for no preference.

    A workspace whose definition names no `log_dir` writes to `<root>/<workspace directory name>` instead of to its own `logs/` — one export, and every workspace on the machine lands in the bucket, each in a directory of its own (`_workspace.layout.resolve_log_dir`).

    **A default, never an override, which is what admits it at all.** `log_dir` is the one inspect word Steward keeps, and Steward refuses every way of overriding a definition's stated one — there is no `--log-dir`, and `INSPECT_LOG_DIR` is refused at launch. This contradicts none of that: a definition that names a directory is untouched, and what the root answers is the case where the definition names none. What it expresses is *where this machine keeps logs*, which no definition can portably say, since a definition knows neither the machine's root nor the workspace's name.

    `auto` is refused rather than accepted, unlike `log_store`: there is no default root to name, and *unset* already spells "the workspace's own `logs/`".
    """

    @field_validator("log_root", mode="before")
    @classmethod
    def _log_root(cls, value: object) -> object:
        """A location, or `false`. Shaped like `log_store` minus the one word that has nothing to mean here."""
        if value is True:
            raise ValueError(
                "should be a path, or `false` to use none — `true` says nothing "
                "about where"
            )
        if isinstance(value, str) and not value.strip():
            raise ValueError("should be a path, or `false` — not an empty value")
        if value == "none":
            raise ValueError("is `false` now, since `none` reads as a path called none")
        if value == "auto":
            raise ValueError(
                "has no default location to name — leave it unset for the "
                "workspace's own logs/ directory"
            )
        return value

    log_store: str | bool | None = Field(default=None)
    """Where to look for logs this run does not have to produce, `false` for nowhere, or `None` for no preference.

    An index of completed logs keyed on inspect_ai's `task_identifier`, holding paths rather than data — Steward reads it at launch and publishes to it at signoff (execution.md §5.3–5.6). A path, or `auto` for the default location.

    **A key here as well as a variable, and the two do not conflict.** execution.md §5.6 routes the location to the environment on the grounds that a store is a property of the machine rather than of the project, which is true of *where this machine's store lives* and not of *which store this project wants to reuse from*. Both are real questions and the precedence answers them in the right order: the file is the project's preference, a variable is the machine's, and the flag is the invocation's.

    `false` is how a project declines a store the machine configured, and it resolves the same as expressing no preference at all — deliberately, because the two are indistinguishable in effect and inventing a record of the difference would be recording something nothing reads.
    """

    @field_validator("log_store", mode="before")
    @classmethod
    def _log_store(cls, value: object) -> object:
        """A location, or `false`. `true` says nowhere in particular, which is not an answer.

        The empty string is refused for the reason `--log-store ''` is: typing a value and leaving it blank is a mistake, where *saying nothing* already has a spelling.

        `none` is refused by name rather than taken literally. It used to mean *no store* and now means a directory called `none`, so the one reading nobody intends is the one it would silently get.
        """
        if value is True:
            raise ValueError(
                "should be a path, `auto`, or `false` to use no store — "
                "`true` says nothing about where"
            )
        if isinstance(value, str) and not value.strip():
            raise ValueError(
                "should be a path, `auto`, or `false` — not an empty value"
            )
        if value == "none":
            raise ValueError("is `false` now, since `none` reads as a path called none")
        return value

    sync: str | bool | None = Field(default=None)
    """Where the workspace's own files are propagated to, `false` for nowhere, or `None` for the log directory.

    Each tend mirrors `status.md`, `journal.jsonl`, `steward.log`, the definition, and whatever else is at the top level into the run's log directory, so that everything explaining a result sits beside it — which on a machine reachable only through an object store is the whole observability channel (workflow.md, *Syncing the workspace out*).

    A path sends them somewhere other than the logs; `auto` is the default said out loud. Passes the admission test the same way `log_store` does: no `eval_set()` argument reaches it, and where the files go is a property of the deployment rather than of the eval.
    """

    @field_validator("sync", mode="before")
    @classmethod
    def _sync(cls, value: object) -> object:
        """A location, or `false`. Shaped exactly like `log_store`, and refused the same way."""
        if value is True:
            raise ValueError(
                "should be a path, `auto`, or `false` to propagate nowhere — "
                "`true` says nothing about where"
            )
        if isinstance(value, str) and not value.strip():
            raise ValueError(
                "should be a path, `auto`, or `false` — not an empty value"
            )
        if value == "none":
            raise ValueError("is `false` now, since `none` reads as a path called none")
        return value

    policies: str | list[str] | None = Field(default=None)
    """Standing rules an agent applies, as prose or as a list of them, or `None` where the project has none.

    The half of the file Steward does not execute. It is typed only enough to be carried and reported: a block scalar for a project whose standards want paragraphs, a list for one whose standards are three sentences, and no attempt to tell them apart. Nothing here parses the text, so a rule Steward cannot act on costs nothing beyond the words.

    Last in the model because it is last in the file a person writes — settings first, then the reasoning nobody has taught Steward to execute yet.
    """

    @field_validator("policies", mode="before")
    @classmethod
    def _policies(cls, value: object) -> object:
        """A list of rules is a list of strings, and a list of anything else is refused with its meaning.

        `mode="before"` for the same reason `samples_ramp` needs it: strict validation would report a list containing an integer as a type error against the whole field, when what the author wants told is which entry is wrong. An empty list is a project that wrote `policies:` and stopped, which is `None` rather than an error.
        """
        if not isinstance(value, list):
            return value
        entries = cast(list[object], value)
        for index, entry in enumerate(entries):
            if not isinstance(entry, str):
                raise ValueError(
                    f"should be text, or a list of it — entry {index + 1} is "
                    f"{type(entry).__name__} rather than text: {entry!r}"
                )
        return entries or None


def read_directives(path: Path) -> Directives:
    """Read a workspace's directives, from the file and the environment.

    Args:
        path: `_steward.yaml`. Need not exist.

    Returns:
        What the workspace asked for. All defaults where neither source said anything — an absent file is a workspace that expressed no preferences, not an error.

    Raises:
        DirectivesError: Either source names a key that belongs elsewhere or one Steward does not know, a value does not validate, the file is not valid YAML or not a mapping, or the workspace still holds a `_steward.md` under the old format.
    """
    settings = _file(path)
    environment = _environment(os.environ)
    settings.update(environment)
    _refuse(settings, name=path.name)

    try:
        return Directives.model_validate(settings)
    except ValidationError as ex:
        raise DirectivesError(
            f"{path.name} is not valid: "
            f"{explain(ex, {key: f'{PREFIX}{key.upper()}' for key in environment})}"
        ) from ex


def parse_setting(key: str, value: str) -> Any:
    """One setting typed on the command line, read exactly as the file's would be.

    The third spelling, and it goes through the same two steps the other two do — `yaml.safe_load` for what the text means, then the field's own validation for whether it is allowed. That is what keeps `--samples-ramp '[40, 300]'`, `samples_ramp: [40, 300]`, and `STEWARD_SAMPLES_RAMP='[40, 300]'` from being three parsers that agree by coincidence, and it is why `--samples-ramp true` is refused with the same sentence about saying nothing about how far.

    Args:
        key: The setting's name, as `_steward.yaml` spells it.
        value: What was typed.

    Returns:
        The validated value, ready for `resolve_pool` or `resolve_interval`.

    Raises:
        DirectivesError: The text is not valid YAML, or the value is not allowed for this setting.
    """
    try:
        loaded: Any = yaml.safe_load(value)
    except yaml.YAMLError as ex:
        raise DirectivesError(f"`{value}` is not a valid value: {ex}") from ex
    try:
        return getattr(Directives.model_validate({key: loaded}), key)
    except ValidationError as ex:
        raise DirectivesError(explain(ex)) from ex


def _file(path: Path) -> dict[str, Any]:
    """What the file said, as a mapping to overlay the environment onto.

    A mapping rather than `Directives` on every path, including the three that say nothing — absent, empty, all comments. Returning parsed settings early would mean a workspace with no file ignored its environment, which is the one arrangement a machine-level variable exists to serve.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        refuse_superseded(path)
        return {}
    except OSError as ex:
        raise DirectivesError(f"{path.name} could not be read: {ex}") from ex
    except UnicodeDecodeError as ex:
        # not an OSError, and an editor that saved as latin-1 is the ordinary
        # way to get one -- without this it is a traceback rather than a
        # message naming the file
        raise DirectivesError(f"{path.name} is not valid UTF-8: {ex}") from ex

    try:
        loaded: Any = yaml.safe_load(text)
    except yaml.YAMLError as ex:
        raise DirectivesError(f"{path.name} is not valid YAML: {ex}") from ex

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise DirectivesError(
            f"{path.name} must be a mapping of settings, not {type(loaded).__name__}"
        )
    return cast(dict[str, Any], loaded)


def _environment(environ: Mapping[str, str]) -> dict[str, Any]:
    """What the environment said, as the same mapping the file produces.

    **The environment is the file, one key at a time.** Each value goes through `yaml.safe_load` and joins the mapping the file built, so strict validation, the refusal table, and the errors that name what arrived all apply to it unchanged — `STEWARD_MAX_WORKERS=yes` fails exactly the way `max_workers: yes` fails, and for the same reason. Nothing here validates; it only decides which variables are Steward's and what their text means.

    It sits between the file and the command line because that is what the three spellings mean: the file is what this project wants, a variable is what this machine or this shell wants, and a flag is what this invocation wants. Narrower wins.

    **An unrecognised `STEWARD_*` variable is refused rather than ignored**, which is the same posture the file takes toward an unknown key and exists for the same failure: a misspelled `STEWARD_SAMPLE_RAMP` that sits inert is a setting someone believes is in force. That makes the prefix a namespace Steward has to be careful with, and two kinds of name under it are skipped rather than read: `RESERVED` names the worker markers, which are not settings at all, and `ALIASES` names inspect's words, which `overrides.py` resolves against a different model.
    """
    settings: dict[str, Any] = {}
    for name in sorted(environ):
        if not name.startswith(PREFIX) or name in RESERVED or name in ALIASES:
            continue
        key = name.removeprefix(PREFIX).lower()
        if key in REFUSED:
            raise DirectivesError(
                f"{name} is not Steward's to set — `{key}` belongs in {REFUSED[key]}"
            )
        if key not in Directives.model_fields:
            raise DirectivesError(f"{name} is not a setting Steward knows")
        # an exported-but-empty variable is unset, the same reading `_timer.env`
        # gives a credential: refusing a shell profile that exports an empty
        # value would be refusing a correct setup
        if not (value := environ[name]).strip():
            continue
        try:
            settings[key] = yaml.safe_load(value)
        except yaml.YAMLError as ex:
            raise DirectivesError(f"{name} is not a valid value: {ex}") from ex
    return settings


def resolve_pool(
    directives: Directives,
    *,
    max_workers: int | None = None,
    max_tasks: int | None = None,
    max_samples: int | None = None,
    stall_after: int | None = None,
    samples_ramp: tuple[int, int] | bool | None = None,
) -> Pool:
    """Resolve what the operator asked of the worker pool.

    Two shapes of chain, and the difference between them is the ownership rule made visible:

    | | |
    |---|---|
    | `max_workers` | the CLI, then `_steward.yaml` (or its variable), then unbounded |
    | `stall_after` | the CLI, then `_steward.yaml`, then the default |
    | `samples_ramp` | the CLI, then `_steward.yaml`, then the default range |
    | `max_tasks` | the CLI, then **the definition**, then unbounded — the file is not a source |
    | `max_samples` | the CLI, then **the definition**, then the default — the file is not a source |

    The last two chains continue inside `resolve_max_tasks` and `resolve_max_samples`, which is why their CLI values pass straight through rather than being filled in here: *no preference* yields to whatever the definition asked for, and a number is an instruction that does not. Both are words `eval_set()` knows, so the definition owns them and this file refuses them by name.

    The first three are Steward's own words, and every one of them can be said three ways — in the file, in a `STEWARD_*` variable, or on the command line — with no exceptions to remember. `stall_after` and `samples_ramp` were flagless for several steps on the argument that a standing property should not be retyped each turn, which described the author's usual case rather than a reason the operator could not have one for the exceptional turn.

    `max_workers` has no default to fall back to, which is not an omission: `None` is the answer, and it means *do not bound this* — a run nobody shaped runs everything, in a process each.

    Args:
        directives: What the workspace's `_steward.yaml` and environment said.
        max_workers: Process count from the command line, or `None`.
        max_tasks: Task concurrency from the command line, or `None`.
        max_samples: Sample concurrency from the command line, or `None`.
        stall_after: Respawn patience from the command line, or `None`.
        samples_ramp: Ramp envelope from the command line, or `None`.

    Returns:
        What the operator asked for, for `reconcile`.
    """
    ramp = samples_ramp if samples_ramp is not None else directives.samples_ramp
    patience = stall_after if stall_after is not None else directives.stall_after
    return Pool(
        max_workers=max_workers if max_workers is not None else directives.max_workers,
        max_tasks=max_tasks,
        max_samples=max_samples,
        # `True` cannot arrive -- the field validator refuses it -- but the model
        # types the field as `bool` for pydantic's sake, and `Pool`'s narrower
        # `Literal[False]` is the honest type downstream
        samples_ramp=ramp if isinstance(ramp, tuple) or ramp is False else None,
        stall_after=patience if patience is not None else DEFAULT_STALL_AFTER,
    )


def resolve_interval(
    directives: Directives, *, tend_interval: int | None = None
) -> int:
    """Resolve how often this workspace should tend.

    The `max_workers` chain, one key over: the command line, then `_steward.yaml` or its variable, then the default. An interval is a standing property of the host, so the file is a real source for it — unlike `max_samples`, whose source is the definition.

    Args:
        directives: What the workspace's `_steward.yaml` and environment said.
        tend_interval: Seconds from the command line, already parsed, or `None`.

    Returns:
        Seconds between tends.
    """
    if tend_interval is not None:
        return tend_interval
    if directives.tend_interval is not None:
        return directives.tend_interval
    return DEFAULT_TEND_INTERVAL


def declared_notification(environ: Mapping[str, str]) -> str | bool | None:
    """`STEWARD_NOTIFICATION` alone, for a caller that has no `Directives` to ask.

    **Only for the path where `_steward.yaml` would not parse**, which is also one of the conditions most worth notifying somebody about — so the channel has to be resolvable without the file that would normally carry it. Everything else goes through `read_directives`, where this variable arrives as a field like every other.

    Never raises, for the same reason: this is reached because something already failed, and a second failure on the way to reporting the first is how a broken workspace goes silent. A value the field would refuse reads here as *no preference*, leaving `INSPECT_EVAL_NOTIFICATION` to answer.

    Args:
        environ: The environment to read, normally `os.environ`.

    Returns:
        A target, `False` where the operator declined, or `None` where the variable is unset, empty, or unusable.
    """
    if not (raw := environ.get(f"{PREFIX}NOTIFICATION", "")).strip():
        return None
    try:
        # through the shared parser rather than beside it, so the one validator
        # answers here too -- a second reading of the same variable is a second
        # set of rules waiting to disagree with the first
        return cast("str | bool | None", parse_setting("notification", raw))
    except DirectivesError:
        return None


def resolve_log_root(
    directives: Directives, *, log_root: str | bool | None = None
) -> str | None:
    """Resolve the root this machine keeps eval logs under.

    The same chain as every other setting — the command line, then `_steward.yaml` or its variable, then none — and none is the default, which is what leaves a workspace that has never heard of a root writing to its own `logs/`.

    **The variable beating the file is the shipped ordering, and it is the right one here too.** `log_root: false` in a committed `_steward.yaml` does not survive a machine that exports `STEWARD_LOG_ROOT`, exactly as `log_store: false` does not: the file is the project's preference and the variable is the machine's, and a machine that has said where its logs go is answering a question the project cannot. `--no-log-root` is how one launch overrules both.

    Resolved at launch and nowhere else. The answer goes into the committed manifest as a *directory* rather than as a root, so nothing downstream reads this again (`Manifest.log_dir`).

    Args:
        directives: What the workspace's `_steward.yaml` and environment said.
        log_root: A location from the command line, `False` to use none, or `None`.

    Returns:
        A location, or `None` for none. Unresolved — `resolve_log_dir` is what turns it into a directory, and the directory is created by the run that writes into it.
    """
    resolved = log_root if log_root is not None else directives.log_root
    return resolved if isinstance(resolved, str) else None


def resolve_log_store(
    directives: Directives, *, log_store: str | bool | None = None
) -> str | None:
    """Resolve where this run may find logs it does not have to produce.

    The same chain one key over: the command line, then `_steward.yaml` or its variable, then nowhere. Nowhere is the default because a store is an optimisation — its absence costs time and never correctness — so a workspace that has never heard of one behaves exactly as it did.

    **`false` and *no preference* resolve alike, and the collapse is deliberate.** A project declining the machine's store and a machine with no store both run against no store, and the two are indistinguishable in effect; recording the difference would be recording something nothing reads. What `false` buys is the *ability* to decline, which is only meaningful because the file and the variable now stack.

    Args:
        directives: What the workspace's `_steward.yaml` and environment said.
        log_store: A location from the command line, `False` to use none, or `None`.

    Returns:
        A location, or `None` for no store. Unresolved — a path is not checked, because a store is created when something first publishes to it rather than when a launch mentions it.
    """
    resolved = log_store if log_store is not None else directives.log_store
    return resolved if isinstance(resolved, str) else None


def refuse_superseded(path: Path) -> None:
    """Refuse a workspace that still holds the old file, rather than running as if it said nothing.

    Call only where `_steward.yaml` is absent, which the two callers each establish their own way — a workspace that has been converted never pays for this, and one that kept the old file beside a converted new one is not in trouble. The failure it prevents is the quiet one: an unconverted workspace parses perfectly — as a workspace with no directives at all — and every standing rule in it stops applying with nothing said. Silence is the wrong answer to a file somebody wrote on purpose.

    **`steward init` is the second caller, and needs this more than reading does.** `init` completes a partial workspace by writing what is missing, and `_steward.yaml` missing is exactly the state an unconverted workspace is in — so without this, init writes an empty one, the refusal below stops being reachable *forever*, and the standing rules the author wrote are lost with nothing said. A read at least fails loudly the first time.

    Args:
        path: The `_steward.yaml` that is absent.

    Raises:
        DirectivesError: The superseded `_steward.md` is there instead.
    """
    old = path.with_name(SUPERSEDED)
    if old.exists():
        raise DirectivesError(
            f"this workspace has a {SUPERSEDED}, which Steward no longer reads — "
            f"rename it to {path.name} and convert it to YAML, moving the prose "
            f"below the front matter into a `policies:` key"
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


def explain(error: ValidationError, sources: Mapping[str, str] = {}) -> str:
    """A validation failure as one clause per offending key.

    Three departures from pydantic's own wording, all because the default describes the mechanism rather than the mistake. *Extra inputs are not permitted* becomes a sentence about the key. A **type** failure names what arrived, which is the whole value of validating strictly: someone who wrote `max_workers: yes` needs to see `True` to understand that YAML rewrote it, and *should be a valid integer* on its own does not tell them.

    And a key the environment supplied is named as the variable it came from. Same reasoning one level out: an author staring at a file that does not contain the offending value needs to be told where it does come from, or the message sends them to the wrong place. `sources` carries the variable name rather than deriving it, because inspect's spellings are data (`overrides.py`) and only the caller knows which one was read.
    """
    clauses: list[str] = []
    for item in error.errors():
        location = item["loc"]
        key = ".".join(str(part) for part in location)
        # keyed on the field rather than the whole path: a union member fails as
        # `epochs.int`, and the reader needs the name they can act on
        named = sources.get(str(location[0]) if location else "", key)
        message = item["msg"].replace("Input should be", "should be", 1)
        if item["type"] == "extra_forbidden":
            clauses.append(f"`{named}` is not a setting Steward knows")
        elif not key:
            clauses.append(item["msg"])
        elif item["type"].endswith("_type"):
            clauses.append(f"`{named}` {message}, not {item['input']!r}")
        else:
            clauses.append(f"`{named}` {message}")
    # a union field fails once per member, and two clauses naming the same
    # variable is the mechanism showing through rather than the mistake
    return "; ".join(dict.fromkeys(clauses))
