"""`signoff` — the attestation, and the end of the run.

Steward can compute that no anomaly is open. Only a person can say **I accept these results**, and conflating those is how a run ends up looking certified because a machine ran out of things to flag (workflow.md §13). So this verb records who, when, the manifest digest it covered, and the exceptions accepted by name — and then stops the run: it curates the superseded attempts out of `logs/` and takes the timer down.

**It runs a real turn rather than a preview, and holds the claim across both.** Three things follow from that and none of them would from a `status`. The artifacts a person is about to attest to are current at the moment they sign — `status.md`, `anomalies.md`, and any acceptance ruling that landed since the last tend, whose status flip is what makes the logs on disk agree with the decision. The gate judges an executed turn rather than a projection of one. And curation gets the still directory it needs, because the tend that could have spawned a worker into it is the same tend the claim is being held for. This is `launch`'s composition read backwards — capture, gate, commit, tend at one end; tend, gate, curate, sign at the other — and the claim spans it for the same reason.

**It is an attestation, not access control.** What is the human's is the *decision*, never the keystroke: an agent that notices a run is ready, tells them, and carries out their answer is doing its job, and it records their name in `by` exactly as `rule` does for a ruling it is relaying. So this records the signer rather than gating the caller, and a signature nobody asked for is visible rather than prevented — the bargain a commit author line already makes.

**Signing does not commit the journal**, and that stays the human's job (workflow.md §18 q4). What this verb owes instead is that at the moment it returns, the record is complete and quiescent — nothing further will be appended without somebody asking for it — so a commit taken any time afterwards captures the same thing. It says so on the way out.
"""

from collections.abc import Set
from dataclasses import dataclass, field
from typing import cast

from inspect_ai._util.file import basename
from inspect_scout import Summary as ScanSummary

from .._evalset.manifest import ManifestError, read_manifest
from .._notify import (
    Kind,
    Post,
    channel_apprise,
    establish_channel,
    send_post,
)
from .._scan import finalize_scan, scan_dir_location
from .._store import Published, StoreError, open_store, store_location
from .._tend import TendError, TendResult, tend
from .._tend.turn import SCAN_FOLD_RESTORED
from .._timer import TimerError, disarm
from .._workspace import (
    ACTION,
    SIGNOFF,
    Claim,
    Directives,
    DirectivesError,
    Held,
    JournalEvent,
    Signature,
    Workspace,
    acquire,
    append_event,
    read_directives,
    read_journal,
    read_signoff,
    resolve_log_store,
)
from .curate import Curated, curate, plan
from .gate import STANDING, UNFINALIZED, UNSIGNED, Blocker, check

PUBLISHED = "published"
"""The `ACTION` a publication is recorded under, and the workspace's provenance record.

Its `written` list is what makes withdrawal answerable: *this project put these logs in this store*. Accumulating, since a publication does not expire. Folded by `_publications`.
"""

WITHDRAWALS = "withdrawals"
"""The `ACTION` this workspace records an unpaid withdrawal under.

A snapshot rather than a delta: the newest event for a store is what is still owed to it, and an empty list is the debt cleared. Folded by `_pending`.
"""


class SignoffError(Exception):
    """A signoff could not be completed.

    A message for a person, never a traceback. A refusal at the gate is **not** one of these — that is an outcome with its blockers attached, and the blockers are the whole point of it.
    """


@dataclass(frozen=True)
class Signoff:
    """What a signoff did, or what it stopped short of doing."""

    turn: TendResult
    """The turn it judged, and the one whose artifacts it signed over."""

    blockers: list[Blocker] = field(default_factory=list[Blocker])
    """Why it refused, all of them. Empty on a signature."""

    signature: Signature | None = None
    """What was recorded, or `None` where the gate refused. The one field that says whether anything happened."""

    curated: Curated | None = None
    """The superseded attempts moved out of `logs/`, or `None` where nothing was signed."""

    disarmed: str | None = None
    """The scheduler taken down, or `None` where nothing was armed."""

    warnings: list[str] = field(default_factory=list[str])
    """Things worth telling the signer that are not refusals — a paused run, journal damage, a log that would not read, a move that failed."""

    published: Published | None = None
    """What `--publish` put into the log store, or `None` where nothing was asked for or nothing was signed."""

    unpublished: str | None = None
    """Why a store this workspace configures holds nothing from this signature, or `None`.

    **Not a warning, because nothing went wrong** — publication is a decision, and declining to make one is a legitimate outcome rather than a failure. What it is is the last moment anybody is looking: the timer is coming down and the run will never tend again, so a store sitting configured and unpublished has to say so here or say so nowhere.
    """

    unverified: str | None = None
    """Why a recorded signature could not be checked against what the terminal fold revealed, or `None` where it was.

    **The third outcome, and the two it is not.** A refusal has no signature; a success has one and every act that follows it. This has the signature — it is in the journal, and nothing about a turn that would not run is evidence about the run — and stops short of the acts that assume it was verified: the timer stays armed so the next turn asks the question, and the caller reports the run as unfinished rather than signed.
    """


def signoff(
    workspace: Workspace,
    *,
    by: str,
    note: str | None = None,
    again: bool = False,
    publish: bool = False,
    break_stale: bool = True,
) -> Signoff | Held:
    """Attest that these results are accepted, and end the run.

    Args:
        workspace: The workspace to sign.
        by: Who is signing. Free text — a name on a document, never a role.
        note: What they want said about it, or `None`. Optional by design: the account of every decision is already in the journal, and a required field here collects *results look good* at scale.
        again: Record a second signature over a run whose first one still stands. The only blocker with an override, because it is the only one that is not about the run.
        publish: Put the signed logs into the configured log store, so another project can reuse them without running the task. **Defaults to off and has no configured default that could turn it on**: exporting somebody's results into a shared cache is a decision a person makes, once, out loud — so what a `_steward.yaml` key would buy here is publication nobody was asked about, which is the one thing this must not do.
        break_stale: Kill a wedged claim holder and take the claim from it.

    Returns:
        What the signoff did, or a `Held` naming the holder that would not give up the claim. A `Signoff` with no `signature` is the gate refusing, which is an answer rather than a failure.

    Raises:
        SignoffError: The turn could not be run, or the timer would not come down.
        ManifestError: What is committed is not a manifest.
    """
    if not by.strip():
        # **caught here rather than left to the record.** `required=True` at the
        # CLI means *present*, not *non-empty*, and `read_signoff` discards a
        # signature with nobody behind it -- so an empty name signed, curated,
        # disarmed the timer, printed a success, and left a run with no
        # attestation at all and nothing left to notice
        raise SignoffError(
            "--by needs the name of whoever accepted these results; a "
            "signature with nobody behind it is not an attestation"
        )
    outcome = acquire(workspace.claim, command="signoff", break_stale=break_stale)
    if isinstance(outcome, Held):
        return outcome

    with outcome as claim:
        return _signoff(
            workspace, claim, by=by, note=note, again=again, publish=publish
        )


def _signoff(
    workspace: Workspace,
    claim: Claim,
    *,
    by: str,
    note: str | None,
    again: bool,
    publish: bool,
) -> Signoff:
    """The signoff itself, with the claim in hand for the whole of it."""
    try:
        result = tend(workspace, claim=claim)
    except TendError as ex:
        raise SignoffError(
            f"the run could not be tended, so there is nothing to attest to: {ex}"
        ) from ex
    if not isinstance(result, TendResult):  # pragma: no cover - claim is in hand
        raise SignoffError("the run claim was given up mid-signoff")

    blockers = [
        blocker
        for blocker in check(result, result.signature)
        if not (again and blocker.kind == STANDING)
    ]
    warnings = _warnings(result)
    if blockers:
        return Signoff(turn=result, blockers=blockers, warnings=warnings)

    # **the moves come first, so the counts the signature records are facts.**
    # A number written before the act it describes is a number that can be
    # wrong, and this one is going into the record that cannot be rebuilt
    superseded = plan(result.observed) if result.observed is not None else []
    curated = curate(superseded, result.log_dir or str(workspace.logs))
    warnings.extend(curated.failures)
    _journal_curated(workspace, curated)

    # **the terminal half of the scan bracket, here and only here.** Every tend
    # folds with `complete=False`, which deliberately prunes nothing and cleans
    # nothing while a sibling worker might still be recording; this is the one
    # moment the run is quiescent and single-writer, so it is the only moment
    # the prune is honest. It runs *after* curation for the same reason
    # curation runs before the counts: the superseded attempts are gone, so
    # what the finalize prunes as orphaned is exactly the rows of the logs
    # that just left (`_scan.summary`, execution.md §4.3)
    #
    # **and it refuses rather than warns**, on `FAILED`'s reasoning and not on
    # `curate`'s: a move that did not happen leaves a tidy-up undone, while a
    # fold that did not happen leaves rows uncompacted — so what the census
    # reported, what the gate judged, and what the signature is about to say
    # was flagged are all over a scan this run has not finished reading. It is
    # ahead of the `SIGNOFF` event, so nothing is unmade by stopping here
    unfinalized, folded = _finalize_scan(workspace, result)
    if unfinalized:
        return Signoff(
            turn=result, blockers=[unfinalized], curated=curated, warnings=warnings
        )

    # **every caveat, not only the ruled ones.** `anomalies.md` admits an
    # acknowledgment whose subject left a mark on the results exactly as it
    # admits a ruling, so a signature drawn from the accepted windows alone
    # would report "no accepted exceptions" over a run whose own caveat list
    # names a stalled task somebody disposed of. One definition of a caveat,
    # and the signature counts what it counts
    exceptions = sorted({caveat.subject for caveat in result.caveats})
    # **and the coverage shortfall, which is a caveat nobody declared.** Every
    # other exception here was decided by a person and carries their reasoning;
    # this one is decided by the flag that got past the gate, and it has to
    # reach the record for the same reason the others do -- a signature reading
    # "no accepted exceptions" over a run whose scanners saw none of it is the
    # sentence this whole verb exists to make impossible
    if uncovered := _uncovered_exception(result, folded):
        exceptions = sorted({*exceptions, uncovered})
    append_event(
        workspace.journal,
        SIGNOFF,
        by=by,
        note=note or "",
        digest=result.manifest_digest or "",
        tasks=result.summary.tasks,
        accepted=len(result.summary.accepted),
        exceptions=exceptions,
        curated=len(curated.moved),
    )
    signature = Signature(
        by=by,
        note=note or "",
        ts=_recorded(workspace),
        digest=result.manifest_digest or "",
        exceptions=tuple(exceptions),
    )

    store = _store_location(workspace)

    # **a second turn, and it is what makes the artifacts true.** The turn
    # above ran before the signature existed, so everything it wrote and
    # propagated -- `status.md`, `anomalies.md`, the observation -- says the run
    # is finished and waiting to be accepted. Nothing would ever correct that:
    # the disarm below is the last act, and a signed run never tends again. So
    # the record is re-rendered and re-synced now that the journal holds the
    # signature, and it posts nothing on its own (`notify._kind` returns None
    # for a signed run), which leaves the terminal message this verb's to send
    signed, ran, stale = _rerender(workspace, claim, result)
    warnings.extend(stale)

    # **and it is the turn that can find out the signature no longer holds.**
    # Everything between the gate and here can put something in front of the
    # run that the gate never saw — the terminal finalize above folds rows for
    # the first time if every earlier fold failed, and what comes out is a
    # window nobody has ruled on, or a parquet nobody can read. Reporting
    # success over either would be the worst of both: a signature in the
    # journal, a hole with no name on it, a disarmed timer, and nothing left to
    # notice. So the gate is asked again, over the state the finalize revealed
    if ran and (revealed := _revealed(signed)):
        return Signoff(
            turn=signed,
            blockers=[
                Blocker(
                    kind=UNSIGNED,
                    summary=(
                        "the signature was recorded, and folding the last of "
                        "the scan results put something in front of this run "
                        "that the gate had not seen"
                    ),
                    remedy=(
                        "answer what is named below, then "
                        "`steward signoff --by NAME --again`"
                    ),
                ),
                *revealed,
            ],
            curated=curated,
            unpublished=_withheld(store, publish),
            warnings=warnings,
        )

    # **and where that turn could not run, the timer is what stays behind.**
    # The check above is the only thing that ever looks at what the finalize
    # compacted, so a turn that raised leaves the question open rather than
    # answered — and the one act that makes an open question discoverable is
    # the tend that would ask it again. Taking the timer down here is what
    # would turn *unverified* into *unnoticeable*, so it is not taken down, and
    # the signature stands: it is in the journal, and a turn that would not run
    # is not evidence about the run (`_rerender`)
    if not ran and scanned(result):
        return Signoff(
            turn=signed,
            signature=signature,
            curated=curated,
            unpublished=_withheld(store, publish),
            warnings=warnings,
            unverified=(
                "the scan results were folded and then nothing could re-read "
                "them, so whether anything was flagged is unsettled — the "
                "signature is recorded and the timer is deliberately still "
                "armed, so the next turn asks the question this one could not"
            ),
        )

    # **publication is last of the acts that leave this project, and it is
    # *behind* the two returns above rather than beside the signature.** What a
    # store row claims is that a result may be reused sight-unseen, which is
    # exactly the claim the terminal fold can still overturn: it folds rows for
    # the first time if every earlier fold failed, and what comes out is a
    # window nobody has ruled on. Publishing before that check exported results
    # into a shared cache and *then* told the operator nothing had been signed
    # -- the failure §5.5 exists to prevent, arriving through the one door left
    # open. So nothing reaches the store until the run is signed and verified
    published = _publish(workspace, result, curated, store, publish, warnings)
    _withdraw(workspace, curated, store, warnings)
    unpublished = _unpublished(store, publish, published, warnings)

    # **after the signature, and it raises where a failed move does not.** A
    # move that did not happen leaves a tidy-up undone; a timer that stayed
    # armed leaves a signed run spending money against an explicit instruction,
    # every ten minutes, with nobody expecting it to
    try:
        disarmed = disarm(workspace)
    except (TimerError, OSError) as ex:
        raise SignoffError(
            f"the run was signed, and its timer could not be taken down: {ex} — "
            f"it is still tending, so remove it with `steward timer disarm`"
        ) from ex

    _post(workspace, signed, signature)
    return Signoff(
        turn=signed,
        signature=signature,
        curated=curated,
        published=published,
        unpublished=unpublished,
        disarmed=disarmed,
        warnings=warnings,
    )


def _revealed(signed: TendResult) -> list[Blocker]:
    """What the post-signature turn found that the gate never saw.

    **The whole gate rather than the signature's own predicate.** `signed_off` catches a window that opened after the signature and nothing else, and the finalize's other product is a compacted parquet that will not read — which leaves the signature standing, the run reading signed, and an `UNREAD` blocker nobody is ever shown. One reason a run cannot be signed is every reason, here as at the gate itself.

    `STANDING` is dropped because it is this path's success signal rather than a blocker: the run *is* signed and nothing has changed since, which is precisely what was wanted.
    """
    return [
        blocker
        for blocker in check(signed, signed.signature)
        if blocker.kind != STANDING
    ]


def _rerender(
    workspace: Workspace, claim: Claim, before: TendResult
) -> tuple[TendResult, bool, list[str]]:
    """One more turn, so the durable record says what was just decided — and the truth about whether it landed.

    Never at the cost of the signature: it has already landed in the journal, which is the record nothing can rebuild, and a turn that will not run leaves the artifacts stale rather than the attestation undone. So a failure here does not raise.

    **But it is said out loud, and reported rather than inferred.** This was swallowed entirely, and the two acts after it — the disarm, and a success message — then left a run whose only durable snapshot said *finished, waiting to be accepted* forever, with no timer to ever correct it and nothing said to the person who had just signed. A turn that raises is not the only way to end up there: `_write_rendered` swallows an `OSError` by design, so a turn that returns perfectly well proves nothing about the files. Nor does the files' *existence* — a stale `anomalies.md` from an earlier turn is a file, and a caveat list a signature does not match is worse than an absent one. So the turn now says which documents it actually wrote (`TendResult.rendered`), and anything not in that list is named with the command that repairs it.

    **Whether it ran is returned rather than inferred, and the caller cannot do without it.** `before` is the turn that ran *before* the signature existed, so it reads unsigned — and a caller testing the returned turn for the signature would take a second turn that merely failed to run as a signature that was invalidated: it would report `UNSIGNED` over an attestation sitting in the journal, leave the timer armed, and tell the person nothing was signed. There is no state of the run that distinguishes those two, because the difference is not about the run at all.

    Args:
        workspace: The workspace, whose files this rewrites.
        claim: The claim already in hand, so the turn does not go looking for one.
        before: The turn the gate judged, returned unchanged if this one cannot run.

    Returns:
        The turn that saw the signature — or `before` — whether that turn is the post-signature one, and a warning for every artifact that did not catch up.
    """
    warnings: list[str] = []
    try:
        after = tend(workspace, claim=claim)
    except Exception as ex:
        warnings.append(f"the final turn could not run: {ex}")
        after = None
    ran = isinstance(after, TendResult)
    if not isinstance(after, TendResult):
        # a turn that did not run wrote nothing, and `before`'s own record is of
        # the pre-signature write -- so the check has to be over this turn alone
        rendered, after = set[str](), before
    else:
        rendered = set(after.rendered)
    for path in (workspace.status, workspace.anomalies, workspace.analysis):
        if path.name not in rendered:
            warnings.append(
                f"the signature is recorded, but {path.name} could not be "
                f"rewritten — what is on disk still describes this run as "
                f"unsigned, and `steward tend` is what brings it up to date"
            )
    return after, ran, warnings


def _warnings(result: TendResult) -> list[str]:
    """What the signer should be told and is not being refused over.

    **Journal damage is here rather than in the gate, and it is counted rather than described.** The person is being asked to attest to a record, so they are told what of it could not be read — and refusing over it would make a damaged line, which nothing can repair mechanically, into a run nobody may ever sign.
    """
    warnings: list[str] = []
    if result.summary.paused:
        warnings.append(
            "this run is paused — signing accepts it as it stands rather than resuming it"
        )
    if damage := len(result.journal_damage):
        warnings.append(
            f"{damage} journal line{'s' if damage != 1 else ''} could not be "
            f"read, so this signature covers a record with holes in it"
        )
    if result.fold_failing:
        # a warning rather than a refusal, on the journal damage's grounds: a
        # store that will not accept a write is not something the signer can
        # repair by ruling on anything. What they are owed is knowing that the
        # scan results this signature covers may be behind the run
        warnings.append(
            f"the scan results have not folded since {result.fold_failing}, so "
            f"any sample flagged after then is not in what you are signing"
        )
    # an unreadable log is **not** here: it is a hole nobody has sized, so the
    # gate refuses over it until somebody names it (`gate.UNREAD`), and once
    # named it is a caveat rather than a warning. Saying it in both places would
    # tell the signer twice about the one thing they have already answered
    return warnings


def _journal_curated(workspace: Workspace, curated: Curated) -> None:
    """Record every log this signoff moved, as one counted action.

    One event rather than one per log, unlike the tend's own archiving: a tend archives an orphan when it meets one, and a reader wants that beside the turn it happened in. Curation is a batch by construction — it is the one moment "superseded" is unambiguous — and *what this signature covered* is one fact, so it is written once with the whole list inside it (workflow.md §13.1, *Signoff reports what it moved*).

    Never raises over an empty pass: a run whose every task ran once has nothing to curate, and a `curated: 0` line in the history is noise about something that did not happen.
    """
    if not curated.moved:
        return
    append_event(
        workspace.journal,
        ACTION,
        action="curated",
        logs=[
            {"from": log.location, "to": destination, "task": log.identifier}
            for log, destination in curated.moved
        ],
    )


def _store_location(workspace: Workspace) -> str | None:
    """The log store this workspace configures, or `None`.

    **Re-resolved here rather than read back off the committed manifest**, and the two questions are genuinely different. What a launch recorded is *which store this run read from*, months ago on whatever machine ran it; what publication needs is *where this machine's store is now*, and a person is standing here to have gotten that wrong in front of. The resolution is the ordinary one — the file, then the variable — minus the flag, because there is no `--log-store` on this verb.

    **Resolved to a location rather than left as the setting**, which matters here more than anywhere: this is the identity a warning names, the journal records, and `_pending` matches a debt against. A relative `log_store` that stayed relative would be a different store to a signoff typed in a subdirectory than to the launch that read it — and a ledger keyed on the setting rather than the place would then hand one store's debt to another.
    """
    directives = _directives(workspace)
    location = resolve_log_store(directives) if directives is not None else None
    return store_location(location, workspace.root) if location is not None else None


def _publish(
    workspace: Workspace,
    result: TendResult,
    curated: Curated,
    location: str | None,
    publish: bool,
    warnings: list[str],
) -> Published | None:
    """Put the signed logs into the store.

    **What is published is what `logs/` holds *after* curation, which takes two filters rather than none.** The observation in hand was read before the moves, so reading `current` off it publishes a set the directory no longer has. **Orphans** are the sharp half: an identifier the definition no longer names has a current log, `plan` archives every one of its attempts including that one, and a signature does not cover any of them. Publishing straight off the observation therefore exported results the attestation excludes — and, where the move had already landed, failed partway through the batch on a path that was no longer there, after copying some of the valid logs. So the set is narrowed by both things this function knows exactly: **a manifest row** (`task.task is not None`, which is what an orphan lacks) and **not among what curation just moved**.

    **Including the tasks carrying accepted exceptions.** A signature is a signature: two samples accepted as errored is a legitimate result with a caveat, and the caveat lives in this project's `anomalies.md` and travels nowhere. That is a hole, it is accepted knowingly, and the alternative — withholding results a person explicitly accepted — makes the store lie in the other direction about what a project produced.

    **What actually reached the store is journalled by name, and that record is what `_withdraw` reads.** A count is enough to report a publication and is not enough to undo one: withdrawal has to know *which store holds which log because this project put it there*, and nothing else in the workspace can answer that afterwards. So the event carries `written` — the logs this call itself wrote, which for a directory store excludes any that were already present under their own name and therefore belong to whoever produced them.

    Args:
        workspace: The workspace, for the journal.
        result: The turn that was signed, for the logs it observed.
        curated: What curation just archived, so those are not published.
        location: The store, or `None` where none is configured.
        publish: Whether publication was asked for. **Publication only** — withdrawal is not this flag's to authorise and does not pass through here.
        warnings: Accumulated warnings, appended to in place.

    Returns:
        What was published, or `None` where publication was not asked for or failed.
    """
    if location is None or not publish:
        return None
    moved = {log.location for log, _ in curated.moved}
    try:
        store = open_store(location, root=workspace.root)
        published = store.publish(_publishable(result, moved))
    except StoreError as ex:
        warnings.append(f"nothing was published to {location}: {ex}")
        return None
    append_event(
        workspace.journal,
        ACTION,
        action=PUBLISHED,
        store=location,
        kind=published.kind,
        logs=published.count,
        failed=len(published.failed),
        written=sorted(published.written),
    )
    _partial(location, published, warnings)
    return published


def _withdraw(
    workspace: Workspace,
    curated: Curated,
    location: str | None,
    warnings: list[str],
) -> None:
    """Take this project's own rows back out for the attempts curation just archived.

    **Not `--publish`'s to authorise, which is where this started.** The two acts were gated together — the store reached publication as `store if publish else None` — so removing a superseded result was conditional on somebody asking to add new ones. They are not the same permission: publication *exports* this project's results, which is why it is prompted and never automatic; withdrawal removes a row this project itself wrote, for a log it has just archived, and exports nothing. A project that published last month and this month curates that attempt away without the flag was leaving the store to serve the log it had just replaced.

    **And it is withdrawn from the store that holds it, which is not the same as the store configured today.** Withdrawing `curated.moved` from whatever `log_store` currently says had two failures pulling in opposite directions. Repointing a workspace from A to B left the row in A untouched forever, because nothing would ever ask A about it again. And in the other direction — the sharper one — a directory store matches on the log's own filename, so a project that **reused** a log from a shared store and later archived that attempt would move the *producer's* copy into `withdrawn/`, ending reuse of it for everybody, over a log it had never published. So the ledger is the authority: `_publications` folds what this workspace wrote and where, and each store is asked only about its own.

    **The journal is the provenance record and it needs no publisher field**, because it is this workspace's journal: anything in it was written here. What it could not answer before was *which* logs, which is why `_publish` now records them by name.

    **Withdrawal is skipped where nothing is owed**, which is the common case and not only an optimisation: upstream's removal narrates itself to flow's own console whatever it is told about verbosity, so calling it over an empty list would print another tool's voice through the middle of a signoff that cleared no rows. It also means a store this project has finished with is never reopened, and a signoff owing nothing touches no store at all.

    Args:
        workspace: The workspace, for the ledger and the journal.
        curated: What curation just archived.
        location: The store configured now, or `None` — carried only because a debt may be outstanding against it from a signoff that never got to publish.
        warnings: Accumulated warnings, appended to in place.
    """
    moved = {log.location for log, _ in curated.moved}
    # read once: the two folds below are over the same file, and a second read
    # could see a journal something appended in between
    events = read_journal(workspace.journal).events
    mine = _publications(events)
    for store in sorted(set(mine) | ({location} if location is not None else set())):
        pending = set(_pending(events, store))
        owed = (moved & mine.get(store, set[str]())) | pending
        if not owed:
            continue
        try:
            open_store(store, root=workspace.root).withdraw(sorted(owed))
        except StoreError as ex:
            _deferred(workspace, store, owed, warnings, ex)
        else:
            _cleared(workspace, store, pending)


def _publications(events: list[JournalEvent]) -> dict[str, set[str]]:
    """Which logs this workspace has put in which store, folded over every signoff.

    **Accumulating rather than newest-wins**, and the two folds beside each other are worth telling apart. A pending debt is a *state* — the newest word about a store is the whole answer, and an empty list ends it. A publication is an *event*: a log published two signoffs ago is still in the store two signoffs later, so the record only ever grows, exactly as `read_launched` accumulates for the same reason. Reading only the newest event would forget every log but the last batch, and forgetting a publication means declining to withdraw it.

    Args:
        events: Events in file order, as `read_journal` returns them.

    Returns:
        Store location to the logs this workspace wrote there, by the location they had in `logs/` — which is what `curated.moved` reports and what withdrawal matches on. A store this workspace never published to is absent.
    """
    publications: dict[str, set[str]] = {}
    for event in events:
        if event.type != ACTION or event.payload.get("action") != PUBLISHED:
            continue
        store = event.payload.get("store")
        written = event.payload.get("written")
        if not isinstance(store, str) or not isinstance(written, list):
            # a payload this version does not understand is data, not damage
            continue
        publications.setdefault(store, set()).update(
            one for one in cast(list[object], written) if isinstance(one, str)
        )
    return publications


def _publishable(result: TendResult, moved: Set[str]) -> list[str]:
    """The current logs a signature covers: a manifest row each, and none just archived."""
    if result.observed is None:
        return []
    return [
        task.current.location
        for task in result.observed.tasks
        if task.task is not None
        and task.current is not None
        and task.current.location not in moved
    ]


def _partial(location: str, published: Published, warnings: list[str]) -> None:
    """Say so where a publication landed some of its logs and not the rest.

    **The store copies one log at a time and nothing wraps them**, so a batch that stops partway has already put logs somewhere a reader will find them. That used to raise, which meant the caller reported *nothing was published* about a store holding all but the last few — a failure announced as its exact opposite, with no record of what had landed. The count is now what the signature covers and this is the other half of it.
    """
    if not published.failed:
        return
    total = published.count + len(published.failed)
    names = ", ".join(basename(one) for one in published.failed[:3])
    more = f" and {len(published.failed) - 3} more" if len(published.failed) > 3 else ""
    warnings.append(
        f"{published.count} of {total} logs reached {location} — {names}{more} "
        f"did not, and are in this workspace only. Sign again with --publish "
        f"--again once the store will take them"
    )


def _pending(events: list[JournalEvent], location: str) -> list[str]:
    """Logs an earlier signoff owed this store and could not withdraw.

    **The newest word for *this* store wins, and the qualifier is the whole fold.** A workspace can be repointed between signoffs, so the most recent withdrawal event may be about somewhere else entirely; stopping at it would drop a debt still owed to a store this one is still square with. Scanning back for this location instead means a store's ledger survives a detour to another one, and a signoff that cleared its debt writes the empty list that ends the scan.

    Args:
        events: Events in file order, as `read_journal` returns them.
        location: The store being asked about, resolved.

    Returns:
        Logs still to withdraw, or empty where the store is square with this project.
    """
    for event in reversed(events):
        if event.type != ACTION or event.payload.get("action") != WITHDRAWALS:
            continue
        if event.payload.get("store") != location:
            continue
        logs = event.payload.get("logs")
        if not isinstance(logs, list):
            # a payload this version does not understand is data, not damage
            return []
        return [one for one in cast(list[object], logs) if isinstance(one, str)]
    return []


def _deferred(
    workspace: Workspace,
    location: str,
    owed: Set[str],
    warnings: list[str],
    ex: StoreError,
) -> None:
    """Record a withdrawal that did not happen, so a later signoff finishes it.

    **Journalled rather than left to be noticed.** The logs are in `logs-archive/` by now and nothing rediscovers them: `plan` works from what `logs/` holds, so an archived attempt is out of every later signoff's reach, and the store would go on serving a superseded result with the only trace of it a warning that scrolled past once. Writing the debt down is what makes the retry in `_publish` possible at all.

    **The whole set each time, not the difference.** This is a snapshot fold — the newest event for a store is the complete answer — which is `read_smoked`'s shape and is what lets a reader stop at the first match instead of replaying the file.
    """
    append_event(
        workspace.journal,
        ACTION,
        action=WITHDRAWALS,
        store=location,
        logs=sorted(owed),
    )
    warnings.append(
        f"{len(owed)} superseded log(s) could not be withdrawn from {location}: "
        f"{ex} — until they are, that store can hand out a result this project "
        f"has replaced. It is recorded, and the next signoff tries again"
    )


def _cleared(workspace: Workspace, location: str, pending: Set[str]) -> None:
    """Close out a debt a previous signoff recorded, once it is actually paid.

    Written only where there was one, so the ordinary signoff — which owes nothing and withdraws what it just archived — adds no event to a journal a person reads.
    """
    if pending:
        append_event(
            workspace.journal, ACTION, action=WITHDRAWALS, store=location, logs=[]
        )


def _unpublished(
    location: str | None,
    publish: bool,
    published: Published | None,
    warnings: list[str],
) -> str | None:
    """Why a store holds nothing from this signature, when something might have.

    Two different silences, and only one of them is fine.

    **Nobody asked**, which is the ordinary outcome and not a failure: publication is a decision, and declining to make one is an answer. It still gets a line, because this is the last moment anybody is looking — the timer comes down immediately below and a signed run never tends again, so a configured store sitting unwritten says so here or is never mentioned again.

    **Somebody asked and there was nowhere to put it**, which is a failure and used to be silent: `--publish` with no store resolved returned no publication *and* suppressed the line above, on the grounds that the operator had already decided. So a signoff typed with `--publish` succeeded, disarmed, and published nothing, with nothing said. It warns now — and names the likeliest cause, which is a run launched under a one-off `--log-store` that `_steward.yaml` never recorded, since this verb re-resolves the location and has no flag of its own to be told again with.

    Args:
        location: The resolved store, or `None`.
        publish: Whether publication was asked for.
        published: What publication did, or `None` where it did not happen.
        warnings: Accumulated warnings, appended to in place.

    Returns:
        The line for a signoff nobody asked to publish, or `None`.
    """
    if not publish:
        if location is None:
            return None
        return (
            f"a log store is configured at {location} and nothing was published "
            f"to it — publication is a decision, and this signoff was not asked "
            f"to make one. `steward signoff --by NAME --publish --again` records it"
        )
    if location is None:
        warnings.append(
            "--publish was given and no log store is configured, so nothing was "
            "published — set `log_store` in _steward.yaml or STEWARD_LOG_STORE "
            "and sign again with --publish --again. A `--log-store` passed to "
            "one launch is not recorded for this verb to find"
        )
    elif published is None:
        # the store failed and `_publish` has already said so; a second line
        # here would report one failure twice in two different voices
        return None
    return None


def _withheld(location: str | None, publish: bool) -> str | None:
    """Why an unfinished signoff published nothing, whatever it was asked for.

    Both early returns above leave a run that is *not* finished — one with a window the finalize revealed, one with a scan nothing could re-read — and neither is a state to export results from. Saying so is worth a line, because `--publish` was typed and did nothing, and the remedy is the same command that answers the blocker.
    """
    if location is None:
        return None
    if not publish:
        return (
            f"a log store is configured at {location} and nothing was published "
            f"to it — this run is not finished"
        )
    return (
        f"nothing was published to {location}: this run is not finished, and a "
        f"store row claims a result may be reused sight-unseen. Sign again with "
        f"--publish once what is named above is answered"
    )


def _recorded(workspace: Workspace) -> str:
    """When the signature landed, read back from the journal rather than minted here.

    The event's own `ts` is the record; a second clock reading would be a second answer, and the one a person quotes should be the one in the file.
    """
    try:
        signature = read_signoff(read_journal(workspace.journal).events)
    except OSError:
        return ""
    return signature.ts if signature is not None else ""


def _directives(workspace: Workspace) -> Directives | None:
    """What `_steward.yaml` says, or `None` where it will not parse.

    Both answers are useful to `establish_channel`, and neither is worth raising over on a path whose whole job is announcing something that already happened.
    """
    try:
        return read_directives(workspace.directives)
    except (DirectivesError, OSError):
        return None


def _post(workspace: Workspace, result: TendResult, signature: Signature) -> None:
    """Say that the run was accepted, once, terminally.

    **Sent by the verb rather than by a tend**, because signoff disarms the timer: a run that has just been signed never tends again, so a turn-driven post would be a message nothing is left to send. Never raises — a channel that would not take the news must not unmake a signature that already happened.
    """
    try:
        if establish_channel(workspace, _directives(workspace)) is None:
            return
        if (instance := channel_apprise()) is None:
            return
        exceptions = (
            f"{len(signature.exceptions)} accepted exception"
            f"{'s' if len(signature.exceptions) != 1 else ''}"
            if signature.exceptions
            else "no exceptions"
        )
        send_post(
            instance,
            Post(
                kind=Kind.SIGNED_OFF,
                glyph=result.verdict.value,
                workspace=workspace.root.name,
                title=f"signed off by {signature.by} ({exceptions})",
                lines=[signature.note] if signature.note else [],
            ),
            workspace.log,
        )
    except Exception:
        return


def _finalize_scan(
    workspace: Workspace, result: TendResult
) -> tuple[Blocker | None, ScanSummary | None]:
    """Compact the last of the scan rows and mark the scan complete, or refuse.

    **A finalize that succeeded closes the fold episode, and it has to.** The episode keeps a mid-run `sync(complete=False)` running every turn until one works (`_tend.turn._findings`), which is exactly right until this call lands — after it, the buffer has been cleaned and the summary rebuilt from the rows, and one more mid-run fold would overwrite that summary with `complete=false` and the counts of a buffer that is no longer there. `_rerender`'s turn is the one that would do it, so the edge is journalled here rather than after it. A finalize that *failed* deliberately leaves the episode open: nothing was cleaned, the rows are still owed, and the next turn should go on trying.

    **What the finalize could not do is a refusal and not a warning.** It was a warning on `_rerender`'s reasoning — a filesystem that will not cooperate must not unmake a decision a person made — and that reasoning does not reach this call, which happens *before* the `SIGNOFF` event and so unmakes nothing by stopping. What it costs is not tidiness: rows that were never compacted are rows the census never read, so a signature taken over them says *nothing was flagged* about samples nothing has looked at. `FAILED` is the precedent and the remedy is its: run it again, and one that keeps failing is a defect rather than a state to sign around.

    **The *incompleteness* the finalize reports is deliberately not read here**, and the reason is that its two halves already have better homes than a second predicate over a summary file. A scanner that **errored** is a `scanerror:` window, refused by `OPEN_WINDOW` until it is ruled — read off the compacted rows both this verb and the tend share, so the gate and the census cannot come to disagree. A transcript nothing **reached** is a coverage shortfall (`_tend.coverage`), reported on the table and in the note under it and deliberately **not** refused: what would close it is scout's resume over the scan directory, which Steward has no verb for, so a blocker over a scanner added at a re-launch would wedge the run permanently — the same trap the acknowledgeable `scan_incomplete` item was retired for. The person signing is told the number and decides.

    Args:
        workspace: The workspace, whose journal carries the episode's edges.
        result: The turn the signature is being taken over.

    **What the finalize *returns* is kept, where it used to be dropped on the floor.** It is the only account of the scan that is taken after the prune, and the prune is what makes the difference: curation has just moved the superseded attempts out, so rows naming them are dropped here — which can lower coverage rather than raise it. The turn's own `coverage` was computed before all of that, so a signature drawn from it can record a census wider than the one that survived. Measured: 3 of 4 before, 0 of 4 after.

    Returns:
        The blocker where the finalize failed, and the summary where it worked. Neither where this run does not scan.
    """
    material = result.scan
    if not scanned(result) or material is None:
        return None, None
    assert result.log_dir is not None and result.scan_id is not None
    try:
        summary = finalize_scan(
            log_dir=result.log_dir, scan_id=result.scan_id, scans=material.scans
        )
    except Exception as ex:
        return Blocker(
            kind=UNFINALIZED,
            summary=(
                f"the last of the scan results could not be folded "
                f"({type(ex).__name__}: {ex}), so what was flagged is not "
                f"settled and nothing has been signed"
            ),
            remedy=(
                "run it again — the finalize is idempotent, and one that keeps "
                "failing is a defect to look at rather than a state to sign over"
            ),
        ), None
    if result.fold_failing is not None:
        _journal(
            workspace,
            SCAN_FOLD_RESTORED,
            target=scan_dir_location(
                log_dir=result.log_dir, scan_id=result.scan_id, scans=material.scans
            ),
        )
    return None, summary


def _uncovered_exception(result: TendResult, folded: ScanSummary | None) -> str | None:
    """How much of this run the scanners actually reviewed, as one line in the signature.

    **A caveat nobody declared, and it was reaching the record as silence.** Every other exception here was decided by a person and carries their reasoning; this one is a fact about the evidence, and leaving it out let a signature read *no accepted exceptions* over a completed four-sample run whose census covered none of it. Reproduced. That is the same failure as an unread log, arriving where nobody had put a predicate.

    **Not a refusal, deliberately, and `gate` is where that argument lives** — briefly: nothing in Steward closes a coverage gap, several perfectly correct configurations produce one, and a flag every signoff has to carry is the same as no gate. So the person signing is told the number and the number is recorded. That is the *explicit, durable* half; the decision is the signature itself.

    **Read off the finalize rather than off the turn**, because the two disagree and only one of them is taken after the prune. Coverage on the turn was computed before curation moved the superseded attempts and before the finalize dropped the rows naming them, so it can be wider than the census that survived — 3 of 4 against an actual 0 of 4, measured on a fixture that does exactly this. Per scanner rather than intersected: it is what the finalize counts, it needs no second read of the directory, and it is the more useful sentence anyway — *which* scanner fell short is what somebody would go and look at.

    Args:
        result: The turn signed over, for the sample population the counts are against.
        folded: What the terminal finalize reported, or `None` where this run does not scan.

    Returns:
        One line naming every scanner that reviewed less than the run landed, or `None` where they all reached everything.
    """
    landed = result.coverage.landed
    if folded is None or not landed:
        return None
    short = sorted(
        (name, entry.scans)
        for name, entry in folded.scanners.items()
        if entry.scans < landed
    )
    if not short:
        return None
    said = ", ".join(f"{name} reviewed {scans}" for name, scans in short)
    return f"scan coverage: of {landed} transcripts, {said}"


def scanned(result: TendResult) -> bool:
    """Whether this run has scan results for the terminal fold to act on.

    The one condition, read in both places that need it: what the finalize does, and what a turn that could not run leaves unverified.
    """
    return (
        result.scan is not None
        and result.log_dir is not None
        and result.scan_id is not None
    )


def _journal(workspace: Workspace, action: str, *, target: str) -> None:
    """Journal an episode edge, and never at the cost of the signature (`_tend.turn._mark`)."""
    try:
        append_event(workspace.journal, ACTION, action=action, target=target)
    except OSError:
        return


def committed_manifest(workspace: Workspace) -> None:
    """Refuse early where there is nothing to sign.

    A workspace nobody launched has no manifest, and a `tend` over it raises a message about scheduling — true, and the wrong sentence for somebody who typed `signoff`.

    Raises:
        SignoffError: Nothing has been launched here.
        ManifestError: What is committed is not a manifest.
    """
    try:
        read_manifest(workspace.manifest)
    except FileNotFoundError as ex:
        raise SignoffError(
            "nothing has been launched in this workspace, so there are no "
            "results to accept"
        ) from ex
    except ManifestError:
        raise
    except OSError as ex:
        raise SignoffError(f"the committed manifest could not be read: {ex}") from ex


__all__ = ["Signoff", "SignoffError", "committed_manifest", "signoff"]
