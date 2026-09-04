"""`anomalies.md` — the caveats that reached the final data.

The claims worth defending: the filter is *left a mark*, so a resolved window is not a caveat and an accepted one is; the reason is the decider's own words, verbatim; the membership is complete rather than the journal's capped sample; an acknowledgment that touched the data comes in the second way and one that did not stays out; and the document says the same thing as the line `status.md` carries about the same window, because one function decides what a caveat is.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

from inspect_steward._anomaly.model import (
    Anomalies,
    Anomaly,
    AnomalyState,
    Disposition,
    Evidence,
    Ruling,
    composed_effect,
)
from inspect_steward._evalset.instances import Instance, InstanceBatch
from inspect_steward._tend import collect_markdown, status_markdown
from inspect_steward._tend.anomalies_md import (
    SAMPLES_NAMED,
    anomalies_markdown,
    caveat_line,
    caveats,
    outcomes_table,
)
from inspect_steward._tend.items import STALLED, SYNC_FAILED, UNREADABLE
from inspect_steward._tend.progress import Progress, TaskProgress
from inspect_steward._workspace import ACKNOWLEDGED, Workspace, append_event
from inspect_steward._workspace.journal import Ack

from ..schedule.test_tend import turn
from .test_items import CLASS, erroring, ruling

RULED_AT = "2026-08-31T02:00:00Z"
REASON = "the provider was down all night; these two are not coming back"


def window(
    *,
    class_key: str = CLASS,
    kind: str = "error",
    state: AnomalyState = AnomalyState.ACCEPTED,
    disposition: Disposition = Disposition.EXCLUDE,
    ts: str = RULED_AT,
    count: int = 2,
    refs: frozenset[str] = frozenset(),
    tasks: tuple[str, ...] = ("probe@openai/gpt-4o",),
    samples: tuple[str, ...] = (),
    effect: str = "2 samples excluded from scoring",
) -> Anomaly:
    return Anomaly(
        class_key=class_key,
        kind=kind,
        state=state,
        evidence=Evidence(count=count, tasks=tasks, samples=samples),
        refs=refs,
        ruling=Ruling(
            class_key=class_key,
            disposition=disposition,
            reason=REASON,
            by="kaia",
            ts=ts,
            effect=effect,
        )
        if state in (AnomalyState.ACCEPTED, AnomalyState.RULED)
        else None,
    )


def instance(
    sample_id: str,
    *,
    epoch: int = 1,
    task: str = "probe@openai/gpt-4o",
    eval_id: str = "e1",
    location: str = "logs/a.eval",
) -> Instance:
    return Instance(
        class_key=CLASS,
        ref=f"{eval_id}:{sample_id}:{epoch}:u-{eval_id}-{sample_id}",
        task=task,
        location=location,
        eval_id=eval_id,
        sample_id=sample_id,
        epoch=epoch,
        uuid=f"u-{eval_id}-{sample_id}",
    )


def census(*instances: Instance) -> list[InstanceBatch]:
    return [
        InstanceBatch(
            class_key=CLASS, kind="error", substrate=False, instances=instances
        )
    ]


def rendered(
    anomalies: Anomalies,
    acks: dict[str, Ack] | None = None,
    batches: list[InstanceBatch] | None = None,
    current: dict[str, str] | None = None,
) -> str:
    """The document over the caveats a turn would have computed."""
    return anomalies_markdown(
        caveats(anomalies, acks or {}, batches or [], {}, current or {})
    )


def ack(kind: str, **fields: Any) -> Ack:
    return Ack(
        id=f"{kind}:probe",
        by="operator",
        reason="the sandbox host is gone for the night",
        ts="2026-08-31T03:00:00Z",
        kind=kind,
        subject="probe@openai/gpt-4o",
        summary=fields.get("summary", "has stopped making progress"),
    )


def test_an_accepted_class_carries_the_reason_verbatim() -> None:
    # the ruling's own words are the only account of the decision that
    # survives, so they are quoted rather than paraphrased or reflowed
    document = rendered(Anomalies(settled=(window(),)))

    assert f'"{REASON}"' in document
    assert "2 samples excluded from scoring" in document
    assert "kaia" in document


def test_a_resolved_window_is_not_a_caveat() -> None:
    """The filter is whether it left a mark, and a re-run that passed left none.

    This is the whole distinction the document rests on: 47 failures that
    re-ran clean are not a footnote on the numbers, and listing them would
    make the caveat list something nobody reads.
    """
    resolved = replace(
        window(state=AnomalyState.RESOLVED, disposition=Disposition.RERUN), ruling=None
    )

    assert caveats(Anomalies(settled=(resolved,)), {}) == []
    assert "No caveats" in rendered(Anomalies(settled=(resolved,)))


def test_the_members_are_every_sample_and_not_the_capped_twenty() -> None:
    """The journal caps its evidence at twenty; a record somebody quotes may not.

    *Which samples exactly* is the question an entry exists to answer, so the
    membership comes from the census joined against the window's refs rather
    than from the capped list the journal carries.
    """
    listed = [instance(f"s{n}") for n in range(25)]
    absorbed = window(count=25, refs=frozenset(one.ref for one in listed))

    document = rendered(Anomalies(settled=(absorbed,)), batches=census(*listed))

    for n in range(25):
        assert f"`s{n}:1`" in document
    assert "more" not in document


def test_a_window_past_the_naming_bound_says_how_many_it_is_not_naming() -> None:
    listed = [instance(f"s{n}") for n in range(SAMPLES_NAMED + 10)]
    absorbed = window(
        count=SAMPLES_NAMED + 10, refs=frozenset(one.ref for one in listed)
    )

    document = rendered(Anomalies(settled=(absorbed,)), batches=census(*listed))

    assert "and 10 more" in document


def test_a_window_whose_census_has_gone_quiet_falls_back_to_what_it_recorded() -> None:
    # the logs were curated away, or a worker's record aged out -- the entry
    # says what it can and is honest that the list is partial
    absorbed = window(count=9, refs=frozenset({"e1:gone:1:u-gone"}), samples=("s0:1",))

    document = rendered(Anomalies(settled=(absorbed,)))

    assert "`s0:1`" in document
    assert "and 8 more" in document


def test_a_task_window_lists_attempts_rather_than_pretending_to_have_samples() -> None:
    failed = window(
        class_key="task:no-log",
        kind="task",
        disposition=Disposition.ACCEPT,
        count=1,
        effect="this arm is dropped from the report",
    )

    document = rendered(Anomalies(settled=(failed,)))

    assert "**Attempts**" in document or "**Samples**" not in document
    assert "1 attempt in `probe@openai/gpt-4o`" in document


def test_two_generations_under_one_ruling_are_one_entry_and_two_are_two() -> None:
    """One entry per decision, not per window.

    A class-scoped ruling closes both generations at once — one decision, one
    reason, one entry. Two separate rulings are two decisions, and merging
    those would file one operator's reasoning under another's.
    """
    shared = Anomalies(settled=(window(), replace(window(), generation=2)))
    separate = Anomalies(
        settled=(window(), replace(window(ts="2026-08-31T09:00:00Z"), generation=2))
    )

    assert len(caveats(shared, {})) == 1
    assert len(caveats(separate, {})) == 2


def test_a_merged_entry_carries_every_generation_the_decision_covered() -> None:
    """One entry per decision — and the entry has to mean the whole decision.

    Keeping the first window and skipping the rest would file a class-wide
    ruling under whichever generation happened to be first, so the entry would
    understate the scope of what somebody signed.
    """
    elsewhere = instance("s2", task="second@openai/gpt-4o")
    first = window(count=2, refs=frozenset({instance("s0").ref, instance("s1").ref}))
    second = replace(
        window(
            count=1,
            refs=frozenset({elsewhere.ref}),
            tasks=("second@openai/gpt-4o",),
        ),
        generation=2,
    )

    (caveat,) = caveats(
        Anomalies(settled=(first, second)),
        {},
        census(instance("s0"), instance("s1"), elsewhere),
    )

    # qualified, because the group spans tasks and `s0:1` in one of them is a
    # different sample from `s0:1` in the other
    assert caveat.members == (
        "probe@openai/gpt-4o/s0:1",
        "probe@openai/gpt-4o/s1:1",
        "second@openai/gpt-4o/s2:1",
    )
    assert caveat.scope == (
        "3 samples in `probe@openai/gpt-4o`, `second@openai/gpt-4o`"
    )


def test_one_sample_id_in_two_tasks_is_two_samples() -> None:
    """A class key says nothing about which task raised it.

    An exception type and a raising frame are shared across a sweep by
    construction — the scope line has always been able to read *across 4
    tasks* — so two tasks that each lost `s0:1` are two rows in the results.
    Keying membership on the sample id alone collapsed them into one, and
    undercounted every class that spans tasks.
    """
    here = instance("s0")
    there = instance("s0", task="second@openai/gpt-4o", eval_id="e2")
    absorbed = window(
        count=2,
        refs=frozenset({here.ref, there.ref}),
        tasks=("probe@openai/gpt-4o", "second@openai/gpt-4o"),
    )

    (caveat,) = caveats(
        Anomalies(settled=(absorbed,)),
        {},
        [
            InstanceBatch(
                class_key=CLASS, kind="error", substrate=False, instances=(here, there)
            )
        ],
    )

    assert len(caveat.members) == 2
    assert caveat.scope.startswith("2 samples")


def test_a_decision_the_relaunch_left_behind_is_no_longer_a_caveat() -> None:
    """The census saying *none of these* is an answer, not a silence.

    A class accepted on one attempt, relaunched, and come home clean keeps its
    `accepted` window forever — that is what the fold records — but nothing of
    it is in the results. Reading the empty answer as *the census is
    unavailable* fell back to the window's own history, so the entry went on
    reporting samples that are not in the data and the next signature named it
    as an exception the run no longer carries.
    """
    superseded = [instance(f"s{n}", location="logs/first.eval") for n in range(3)]
    absorbed = window(
        count=3,
        refs=frozenset(one.ref for one in superseded),
        samples=("s0:1", "s1:1", "s2:1"),
    )

    listed = caveats(
        Anomalies(settled=(absorbed,)),
        {},
        census(*superseded),
        current={"probe@openai/gpt-4o": "logs/second.eval"},
    )

    assert listed == []
    assert "No caveats" in anomalies_markdown(listed)


def test_an_accepted_task_failure_a_relaunch_replaced_is_not_a_caveat() -> None:
    """The same narrowing, for the windows that have no samples to narrow.

    A window with no sample population names its attempts, and an attempt a
    relaunch superseded is not in the results — so an accepted task failure
    that a clean re-run replaced would otherwise put a hole in the footnotes
    that the data does not have, and the next signature would name it.
    """
    failed = replace(
        window(
            class_key="task:no-log",
            kind="task",
            disposition=Disposition.ACCEPT,
            count=1,
            effect="this arm is dropped from the report",
        ),
        evidence=Evidence(
            count=1, tasks=("probe@openai/gpt-4o",), logs=("logs/a.eval",)
        ),
    )
    settled = Anomalies(settled=(failed,))

    assert caveats(settled, {}, current={"probe@openai/gpt-4o": "logs/a.eval"})
    assert caveats(settled, {}, current={"probe@openai/gpt-4o": "logs/b.eval"}) == []


def test_an_acknowledgment_whose_condition_cleared_is_not_a_caveat() -> None:
    """An acknowledgment names a condition, not an instant.

    *This log will not read* and *this task has stopped making progress* can
    both stop being true — somebody replaces a truncated upload, a task the
    guard gave up on is relaunched and finishes. The caveat then describes a
    hole the numbers do not have, and the signature names an exception the run
    no longer carries.
    """
    disposed = {"unreadable:x": ack(UNREADABLE)}

    assert caveats(Anomalies(), disposed) != []
    assert caveats(Anomalies(), disposed, cleared={"probe@openai/gpt-4o"}) == []
    # and a subject this cannot place keeps its caveat: losing a footnote
    # nobody asked to lose is the worse of the two mistakes
    assert caveats(Anomalies(), disposed, cleared={"something else"}) != []


def test_the_effect_counts_this_ruling_and_not_the_whole_class() -> None:
    """A class key outlives its generations, and the effect sentence does not.

    Three samples ruled and settled under a prior generation and two open under
    this one make the class's current population five — so composing from it
    recorded *5 samples excluded from scoring* as the effect of a ruling whose
    entry scopes two, one entry contradicting itself in the document written to
    be quoted.
    """
    before = [instance(f"s{n}") for n in range(3)]
    now = [instance(f"s{n}") for n in range(3, 5)]
    anomalies = Anomalies(
        settled=(window(count=3, refs=frozenset(one.ref for one in before)),),
        open=(
            replace(
                window(state=AnomalyState.OPEN, count=2),
                generation=2,
                refs=frozenset(one.ref for one in now),
            ),
        ),
    )
    affected = {CLASS: frozenset(one.ref for one in (*before, *now))}

    composed = composed_effect(anomalies, CLASS, Disposition.EXCLUDE, affected)

    assert composed == "2 samples excluded from scoring"


def test_the_counts_are_what_reached_the_data_not_what_the_window_absorbed() -> None:
    """Three samples that failed twice are three rows in the results, not six.

    A window spans attempts: a re-run ruling puts the second attempt's failures
    beside the first's, and only the current attempt is in the data. Counting
    the window would print *6 samples excluded* three lines under a denominator
    line saying three — a footnote contradicting itself.
    """
    old = [instance(f"s{n}", location="logs/first.eval") for n in range(3)]
    new = [
        instance(f"s{n}", eval_id="e2", location="logs/second.eval") for n in range(3)
    ]
    absorbed = window(count=6, refs=frozenset(one.ref for one in (*old, *new)))

    (caveat,) = caveats(
        Anomalies(settled=(absorbed,)),
        {},
        census(*old, *new),
        current={"probe@openai/gpt-4o": "logs/second.eval"},
    )

    assert caveat.members == ("s0:1", "s1:1", "s2:1")
    assert caveat.scope == "3 samples in `probe@openai/gpt-4o`"
    # the failures the re-runs replaced are named rather than dropped: three
    # samples that failed twice and three that failed once are different
    # findings, and only the first explains the re-run ruling that came before
    assert "3 samples errored the same way" in caveat.what
    assert "6 failures in all, counting re-runs" in caveat.what


def test_an_accepted_scan_failure_says_the_scan_failed_and_not_the_sample() -> None:
    """The sample did not error; its scan did.

    This entry is the report-facing account of what reached the signed data, so
    a line calling an absent verdict a failed sample sends whoever reads it into
    the eval after a problem that is in the scan. The kind has to be named here
    rather than falling through to the error wording, which is the one branch
    the fallback cannot be right about.
    """
    key = "scanerror:scoring_integrity:TimeoutError@openai/_client.py:post"
    instances = tuple(replace(instance(f"s{n}"), class_key=key) for n in range(2))
    batch = InstanceBatch(
        class_key=key, kind="scanerror", substrate=False, instances=instances
    )
    settled = window(
        class_key=key,
        kind="scanerror",
        disposition=Disposition.ACCEPT,
        refs=frozenset(one.ref for one in instances),
        effect="2 transcripts carry no verdict either way",
    )

    (caveat,) = caveats(
        Anomalies(settled=(settled,)),
        {},
        [batch],
        {},
        {"probe@openai/gpt-4o": "logs/a.eval"},
    )

    assert "the scanner threw on 2 transcripts" in caveat.what
    assert "carry no verdict either way" in caveat.what
    assert "errored" not in caveat.what
    # and it is still sample-shaped: two transcripts, named
    assert caveat.members == ("s0:1", "s1:1")


def test_an_acknowledged_stall_reaches_the_file_with_its_reason() -> None:
    # a disposal that touched the data is a caveat, or "removed from the
    # surface" would quietly mean "removed from the record"
    document = rendered(Anomalies(), {"stalled:probe": ack(STALLED)})

    assert "the sandbox host is gone for the night" in document
    assert "the task's results stand as they are" in document


def test_an_acknowledged_propagation_failure_does_not() -> None:
    """The allow-list's negative case, and why it is an allow-list.

    A sync that stopped is machinery: nothing about the numbers changed, so a
    caveat saying otherwise would be a caveat list growing by accident — which
    is a caveat list nobody trusts.
    """
    assert caveats(Anomalies(), {"sync_failed:x": ack(SYNC_FAILED)}) == []


def test_an_acknowledged_unreadable_log_moves_the_denominator() -> None:
    document = rendered(Anomalies(), {"unreadable:x": ack(UNREADABLE)})

    assert "the numbers are over what could be read" in document


def test_the_status_line_and_the_entry_describe_the_same_caveat() -> None:
    """One definition, two renderings — the reason they share a function.

    A reader glancing at `status.md` and a reader quoting `anomalies.md` are
    being told the same thing at two lengths, and the effect sentence is the
    part that must not differ.
    """
    accepted = Anomalies(settled=(window(),))
    (caveat,) = caveats(accepted, {})

    line = caveat_line(caveat)
    document = rendered(accepted)

    assert caveat.effect in line
    assert caveat.effect in document
    assert "exclude by kaia" in line


def test_the_document_says_so_when_nothing_was_accepted() -> None:
    # an absent file is indistinguishable from a tend that never ran, and
    # "no caveats" is worth stating to somebody about to quote the numbers
    assert "No caveats" in rendered(Anomalies())


# --- the by-task table -------------------------------------------------------


def progress(*rows: tuple[str, str, int]) -> Progress:
    return Progress(
        rows=[
            TaskProgress(
                key=f"{name}@{model}",
                name=name,
                model=model,
                identifier=f"id-{name}",
                total=total,
            )
            for name, model, total in rows
        ]
    )


def cells(document: str, name: str) -> list[str]:
    """The row for this task, cell by cell, as a reader of the source sees it."""
    row = next(line for line in document.splitlines() if line.startswith(f"| {name} "))
    return [cell.strip() for cell in row.split("|")][1:-1]


def test_the_by_task_table_is_aligned_in_the_source_and_shortens_its_keys() -> None:
    rows = progress(
        ("cybench", "openai/gpt-5", 50),
        ("swe", "openai/gpt-5", 120),
        ("gaia", "openai/gpt-5", 10),
    )

    lines = outcomes_table(
        {
            "id-cybench": {"zeroed": 2, "errored": 1},
            "id-swe": {"excluded": 3, "scored_early": 2},
            "id-gaia": {},
        },
        rows,
    )

    # padded so that the markdown is a table before anything renders it, the
    # model every row shares named once beneath rather than on every row, and
    # a task with nothing to show given no row
    assert lines == [
        "| task    | zero | nan | error | early | term |",
        "|---------|-----:|----:|------:|------:|-----:|",
        "| cybench |    2 |   · |     1 |     · |    · |",
        "| swe     |    · |   3 |     · |     2 |    · |",
        "",
        "Every task runs `openai/gpt-5`.",
    ]


def test_no_table_where_every_sample_took_the_normal_course() -> None:
    assert outcomes_table({"id-cybench": {}}, progress(("cybench", "m", 50))) == []
    assert "By task" not in anomalies_markdown([], table=[])


def test_the_document_opens_on_the_table_ahead_of_the_caveats() -> None:
    document = anomalies_markdown([], table=["| task |"])

    assert document.index("## By task") < document.index("No caveats")


def test_a_tend_tabulates_what_did_not_take_the_normal_course(tmp_path: Path) -> None:
    workspace = erroring(tmp_path, errors=3, samples=10)
    result = turn(workspace)

    document = workspace.anomalies.read_text(encoding="utf-8")
    row = ["probe", "·", "·", "3", "·", "·"]
    assert cells(document, "probe") == row
    # and the same row, from the same fold, on both of the operator's pages
    assert cells(status_markdown(result), "probe") == row
    assert cells(workspace.status.read_text(encoding="utf-8"), "probe") == row

    ruling(workspace, "exclude", effect="3 of 10 samples excluded from scoring")
    turn(workspace)

    document = workspace.anomalies.read_text(encoding="utf-8")
    assert cells(document, "probe") == ["probe", "·", "3", "·", "·", "·"]


# --- through real turns ----------------------------------------------------


def test_a_tend_writes_the_caveats_beside_the_status(tmp_path: Path) -> None:
    workspace = erroring(tmp_path, errors=3, samples=10)
    turn(workspace)
    ruling(workspace, "exclude", effect="3 of 10 samples excluded from scoring")

    result = turn(workspace)

    document = workspace.anomalies.read_text(encoding="utf-8")
    assert "Regenerated every turn; edits are lost" in document
    assert "3 of 10 samples excluded from scoring" in document
    # the same denominator the agent's table note carries, from one computation
    assert "Scores are over 7 of 10 samples (3 excluded)." in document
    assert "Scores are over 7 of 10 samples (3 excluded)." in collect_markdown(result)
    # in the name the operator's table gives the task -- the same shortening,
    # so neither the identifier's content hash nor the `[default]` the table
    # elides reaches the sentence
    assert "in `probe`" in document
    assert "[default]" not in document


def test_a_status_writes_no_caveats_either(tmp_path: Path) -> None:
    """The read verb writes nothing, and the pair going stale together is why.

    A remote reader detects a stopped timer by noticing these two files stopped
    changing; a preview that stamped one of them fresh would destroy the signal
    while adding nothing.
    """
    from inspect_steward._tend import status

    workspace = erroring(tmp_path)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")

    status(workspace)

    assert not workspace.anomalies.exists()


def test_an_acknowledged_stall_survives_into_the_file(tmp_path: Path) -> None:
    # the fold reads the kind the acknowledging verb recorded, which is what
    # routes a disposal to the caveats rather than only to the journal
    workspace = erroring(tmp_path)
    ruling(workspace, "dismiss")
    payload: dict[str, Any] = {
        "id": "stalled:probe:3",
        "kind": STALLED,
        "subject": "probe@mockllm/model",
        "summary": "has finished nothing new in 3 attempts",
        "by": "operator",
        "reason": "the host is gone; 8 of 10 is enough",
    }
    append_event(workspace.journal, ACKNOWLEDGED, **payload)

    turn(workspace)

    document = workspace.anomalies.read_text(encoding="utf-8")
    assert "the host is gone; 8 of 10 is enough" in document
    assert "has finished nothing new in 3 attempts" in document


def test_caveats_that_cannot_be_written_are_never_a_failed_turn(
    tmp_path: Path,
) -> None:
    workspace: Workspace = erroring(tmp_path)
    ruling(workspace, "exclude", effect="2 samples excluded from scoring")
    workspace.anomalies.mkdir()  # a directory where the file should go

    result = turn(workspace)

    assert result.summary.tasks == 1
