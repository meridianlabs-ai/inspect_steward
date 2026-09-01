"""`signoff` — the attestation, and the end of the run.

Steward can compute that no anomaly is open. Only a person can say **I accept these results**, and conflating those is how a run ends up looking certified because a machine ran out of things to flag (workflow.md §13). So this verb records who, when, the manifest digest it covered, and the exceptions accepted by name — and then stops the run: it curates the superseded attempts out of `logs/` and takes the timer down.

**It runs a real turn rather than a preview, and holds the claim across both.** Three things follow from that and none of them would from a `status`. The artifacts a person is about to attest to are current at the moment they sign — `status.md`, `anomalies.md`, and any acceptance ruling that landed since the last tend, whose status flip is what makes the logs on disk agree with the decision. The gate judges an executed turn rather than a projection of one. And curation gets the still directory it needs, because the tend that could have spawned a worker into it is the same tend the claim is being held for. This is `launch`'s composition read backwards — capture, gate, commit, tend at one end; tend, gate, curate, sign at the other — and the claim spans it for the same reason.

**It is an attestation, not access control.** What is the human's is the *decision*, never the keystroke: an agent that notices a run is ready, tells them, and carries out their answer is doing its job, and it records their name in `by` exactly as `rule` does for a ruling it is relaying. So this records the signer rather than gating the caller, and a signature nobody asked for is visible rather than prevented — the bargain a commit author line already makes.

**Signing does not commit the journal**, and that stays the human's job (workflow.md §18 q4). What this verb owes instead is that at the moment it returns, the record is complete and quiescent — nothing further will be appended without somebody asking for it — so a commit taken any time afterwards captures the same thing. It says so on the way out.
"""

from dataclasses import dataclass, field

from .._evalset.manifest import ManifestError, read_manifest
from .._notify import (
    Kind,
    Post,
    channel_apprise,
    establish_channel,
    send_post,
)
from .._scan import finalize_scan, scan_dir_location
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
    Signature,
    Workspace,
    acquire,
    append_event,
    read_directives,
    read_journal,
    read_signoff,
)
from .curate import Curated, curate, plan
from .gate import STANDING, UNFINALIZED, UNSIGNED, Blocker, check


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
    break_stale: bool = True,
) -> Signoff | Held:
    """Attest that these results are accepted, and end the run.

    Args:
        workspace: The workspace to sign.
        by: Who is signing. Free text — a name on a document, never a role.
        note: What they want said about it, or `None`. Optional by design: the account of every decision is already in the journal, and a required field here collects *results look good* at scale.
        again: Record a second signature over a run whose first one still stands. The only blocker with an override, because it is the only one that is not about the run.
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
        return _signoff(workspace, claim, by=by, note=note, again=again)


def _signoff(
    workspace: Workspace, claim: Claim, *, by: str, note: str | None, again: bool
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
    if unfinalized := _finalize_scan(workspace, result):
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
            warnings=warnings,
            unverified=(
                "the scan results were folded and then nothing could re-read "
                "them, so whether anything was flagged is unsettled — the "
                "signature is recorded and the timer is deliberately still "
                "armed, so the next turn asks the question this one could not"
            ),
        )

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
    for path in (workspace.status, workspace.anomalies):
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


def _finalize_scan(workspace: Workspace, result: TendResult) -> Blocker | None:
    """Compact the last of the scan rows and mark the scan complete, or refuse.

    **A finalize that succeeded closes the fold episode, and it has to.** The episode keeps a mid-run `sync(complete=False)` running every turn until one works (`_tend.turn._findings`), which is exactly right until this call lands — after it, the buffer has been cleaned and the summary rebuilt from the rows, and one more mid-run fold would overwrite that summary with `complete=false` and the counts of a buffer that is no longer there. `_rerender`'s turn is the one that would do it, so the edge is journalled here rather than after it. A finalize that *failed* deliberately leaves the episode open: nothing was cleaned, the rows are still owed, and the next turn should go on trying.

    **What the finalize could not do is a refusal and not a warning.** It was a warning on `_rerender`'s reasoning — a filesystem that will not cooperate must not unmake a decision a person made — and that reasoning does not reach this call, which happens *before* the `SIGNOFF` event and so unmakes nothing by stopping. What it costs is not tidiness: rows that were never compacted are rows the census never read, so a signature taken over them says *nothing was flagged* about samples nothing has looked at. `FAILED` is the precedent and the remedy is its: run it again, and one that keeps failing is a defect rather than a state to sign around.

    The *incompleteness* the finalize reports is deliberately not read here. `complete=False` means scanners errored, and those errors are already counted off the compacted rows every turn (`_scan.findings.ScanFindings.incomplete`) — read once, from the record both this verb and the tend share, rather than by a second predicate here that could come to disagree with it.

    Args:
        workspace: The workspace, whose journal carries the episode's edges.
        result: The turn the signature is being taken over.

    Returns:
        The blocker where the finalize failed, or `None`.
    """
    material = result.scan
    if not scanned(result) or material is None:
        return None
    assert result.log_dir is not None and result.scan_id is not None
    try:
        finalize_scan(
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
        )
    if result.fold_failing is not None:
        _journal(
            workspace,
            SCAN_FOLD_RESTORED,
            target=scan_dir_location(
                log_dir=result.log_dir, scan_id=result.scan_id, scans=material.scans
            ),
        )
    return None


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
