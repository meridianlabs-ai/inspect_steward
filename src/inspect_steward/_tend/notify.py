"""What a turn is worth telling somebody who is not watching.

**One post per turn, at most, whatever changed.** A turn is one moment, and a reader wants one message about it — a tend that finishes three tasks *and* empties the decision queue has one thing to say, not two. So the triggers decide the post's `kind` by precedence and every one of them contributes to its body. This is also what makes the batching free: the tend is already the clock, so a sweep that finishes five tasks in one interval posts once naming five, and a long night tops out at one message per interval (`_notify.post.Kind.PROGRESS`).

**Every trigger is a set diff between turns, never a count.** One item resolving while another arrives is not *no change*, and a task finishing while another is reset is not *no progress* — which is exactly what counts would say. The diffs are computed by the turn (`TendResult.appeared`, `resolved`, `finished`) and consumed here.

**The latches need no state of their own.** A `gate` posts once because `signoff_ready`'s id is keyed on the manifest digest, so it appears in one turn's diff and no later one — and a relaunch that changes the task set mints a new id, which is the re-arming workflow.md §11.1 says a manual convention would get wrong. The one thing with no edge to stand on is a turn that *raises*: it never reaches its observation, so `NOTIFIED` is written for that case alone.

**A channel reaches a person, so it carries the person's items and not the agent's.** An `unreadable` log or a failed action is routed to the agent precisely because a person is not the one who should look at it, and a post that names it anyway is the notification advertising work its reader was told not to do — which is how a channel becomes one somebody mutes. The projections that a *human* reads on purpose (`status.md`, the terminal) still show both, grouped by owner; this is the one surface where a reader did not choose the moment.

**Unless nobody is picking them up, at which point they are the person's after all.** An agent's item is only the agent's while there is an agent — and the thing a person has to do about a workspace nobody has attached to is attach to it. `_unattended` is the whole of that judgement, and it is deliberately made from what the journal already records rather than from a new setting: `collect` stamps the journal, so the collection age is a fact about this run rather than a promise about anyone's tooling.
"""

import time
from dataclasses import replace
from typing import TYPE_CHECKING

from .._notify import (
    GLYPH,
    NARROW,
    WIDTH,
    Kind,
    Post,
    channel_apprise,
    establish_channel,
    send_post,
)
from .._util.duration import seconds_since
from .._workspace import (
    DEFAULT_TEND_INTERVAL,
    NOTIFIED,
    UNDELIVERED,
    Directives,
    DirectivesError,
    Workspace,
    append_event,
    read_directives,
    read_journal,
    read_notified,
)
from .items import FIXED_OWNER, Item, Owner, Verdict, self_healing, verdict_text
from .progress import Progress, short_keys
from .table import progress_table

if TYPE_CHECKING:
    # the turn imports this module to post its own result, so the type it
    # passes can only be named here at type-check time
    from .turn import TendResult

LINES = 8
"""Item or task lines a post carries before it starts counting instead.

A post is read on a phone and skimmed. Past about eight lines nobody is reading them individually, and `status.md` is one command away with all of them — so the ninth is worth less than the space it costs.
"""

ROWS = 12
"""Task rows a post's table carries before it shows the totals alone.

A two-hundred-row monospace block is not a table, it is a wall, and the totals line beneath it is the part that answers *how is the run going* anyway.
"""

SAID = 180
"""Characters of an item's own words a post carries before it trims them.

**Set above everything Steward composes, so that what it bites on is text Steward did not write.** The longest summary in the vocabulary is a parked worker's, at around a hundred and seventy characters, and every other one is well under half of that — each assembled from bounded parts by a line that was written to be read. Three are not: `unreadable`, `degraded` and `action_failed` each embed somebody else's exception, which arrives with an absolute path and a stack-shaped tail and has no length at all in principle. A `ValueError` naming a temporary directory is a diagnosis, and a diagnosis belongs in `status.md` — which holds the whole of it, on a screen, next to everything else that would help. What a phone gets is the sentence.
"""

UNATTENDED_INTERVALS = 2
"""Tends without a collection before the agent's items become the person's.

**Counted in tends rather than in minutes**, because what is being asked is *has an agent had the chance* — and the chances are turns. A workspace tended every ten minutes and one tended hourly are the same run from the agent's side, and a fixed duration would make the second one shout constantly and the first one stay quiet through most of a working day.

Two, because one is the turn the item appeared on. An agent that collects on the tend after an item arrives is an agent doing its job at the ordinary cadence; the horizon has to leave room for that, and one more turn is the smallest amount of room that does.
"""

HOLD_TENDS = 6
"""Tends a landed task's completion waits on an agent's scan investigation before it is posted anyway.

**Why hold at all.** A task whose scan flagged something is not *finished* in the sense a reader takes from a completion post — it is finished and now needs a decision. Posting *finished cybench* and then, twenty minutes later, *a decision needs attention* is two messages about one moment, and the first one is misleading for as long as it stands alone. So where there is an agent to do the investigating, the finish waits for it and the run says one thing once.

**And why hold for a bounded time.** An agent that has attached and then stopped answering would otherwise hold a completion for the rest of the night, which is the failure this whole module exists to prevent. Six is about an hour at the default cadence — long enough for an investigation that involves reading transcripts, short enough that a person who checks after lunch has been told.

Counted in tends rather than minutes for `UNATTENDED_INTERVALS`' reason exactly: the question is how many chances the agent has had, and the chances are turns.
"""


def notify_turn(
    workspace: Workspace, result: "TendResult", channel: str | None
) -> None:
    """Post what this turn is worth posting, if anything, and if anywhere.

    Never raises, and does nothing at all on a quiet turn — the channel is not even built, which is what keeps a settled run from paying for a notifier it has nothing to say to.

    **`channel` is passed rather than re-read, and that is what makes declining work.** `notification: false` silences Steward and deliberately leaves `INSPECT_EVAL_NOTIFICATION` in place for the workers, whose posts are blocking human-in-the-loop prompts. A caller here that asked the environment again would find the fleet's channel and post to it, which is the decline not taking effect at all.

    Args:
        workspace: The workspace, for `steward.log` where a send fails.
        result: The turn that just ran.
        channel: Where Steward posts, as `establish_channel` settled it, or `None` where it posts nowhere.
    """
    if channel is None:
        return
    if (post := turn_post(result)) is None:
        return
    # named here rather than inside `turn_post`, which is pure over a result and
    # has no workspace to ask
    named = replace(post, workspace=workspace.root.name)
    if (instance := channel_apprise()) is None:
        _owed(workspace, result)
        return
    # **owed only where nothing landed.** A channel is a list, and one target
    # failing while another accepted is a reader who *was* told -- retaining the
    # edge for them would repost to the working target every ten minutes until
    # the broken one is fixed, which is the mute this whole module is avoiding
    if send_post(instance, named, workspace.log).reached_nobody:
        _owed(workspace, result)


def _owed(workspace: Workspace, result: "TendResult") -> None:
    """Write down an edge that reached nobody, so the next turn produces it again.

    **Without this a post is a one-shot on a diff that has already been spent.** The observation recording *these items are open now* is what stops them being reported every ten minutes, and it is written before anything is sent — so a notifier that was unreachable for the one minute the gate landed in has consumed the gate, and every turn after it sees a run in which nothing changed. The condition persists and the news about it does not.

    A channel that will not build counts as undelivered for the same reason a refused send does: the reader was not told either way.

    Never raises. A journal write that fails here costs a re-notification, and raising would cost the turn that has already happened.
    """
    try:
        append_event(
            workspace.journal,
            UNDELIVERED,
            items=list(result.appeared),
            complete=list(result.finished),
        )
    except OSError:
        return


def notify_failure(workspace: Workspace, reason: str) -> None:
    """Post that a turn could not run at all, once per distinct reason.

    **The case that is otherwise silent forever.** A malformed `_steward.yaml`, an unreadable log directory, an expired credential — each fails identically every interval, writes nothing anybody reads, and leaves `status.md` frozen at whatever the last good turn said.

    **The file is read here rather than taken from the caller**, because a turn can fail long before it settles a channel — a missing manifest raises in the first few lines — and a caller handing over what it happened to have would leave a workspace whose channel is *only* in `_steward.yaml` unable to report the one condition it most needs to. Where the file is itself what failed, `establish_channel` falls back to the variable on its own.

    Latched on `NOTIFIED` and released by the next turn that reaches its observation, so a run that breaks, is fixed, and breaks again the same way is heard both times.

    **The latch records a post that landed, never one that was attempted.** A notifier that was briefly unreachable would otherwise silence every later attempt at the same persistent failure — the two outages compounding into exactly the silence this function exists to prevent, and the second one invisible because the first swallowed it.

    **Landed anywhere, rather than everywhere.** Where one of several targets refused, somebody has been told; going round again on the next turn would repost to the ones that worked every interval for as long as the broken one stays broken.

    Never raises. A failure on the way to reporting a failure is how a broken workspace goes quiet.

    Args:
        workspace: The workspace whose turn failed.
        reason: What went wrong, as the exception said it.
    """
    try:
        if establish_channel(workspace, _directives(workspace)) is None:
            return
        if reason in _already_said(workspace):
            return
        if (instance := channel_apprise()) is None:
            return
        delivery = send_post(
            instance,
            Post(
                kind=Kind.STOPPED,
                glyph=GLYPH[Kind.STOPPED],
                workspace=workspace.root.name,
                title="the tend could not run (nothing is being scheduled)",
                lines=[reason],
            ),
            workspace.log,
        )
        if delivery.reached_nobody:
            return
        append_event(workspace.journal, NOTIFIED, kind=Kind.STOPPED, subject=reason)
    except Exception:
        # the caller is already reporting a failure to somebody; a second one
        # raised out of the reporting would replace their message with this one
        return


def _directives(workspace: Workspace) -> Directives | None:
    """What `_steward.yaml` says, or `None` where it is what broke.

    Both answers are useful to `establish_channel`, and neither is worth raising over on a path whose whole job is reporting a failure that already happened.
    """
    try:
        return read_directives(workspace.directives)
    except (DirectivesError, OSError):
        return None


def _already_said(workspace: Workspace) -> set[str]:
    """What the latch holds, or nothing where the latch itself cannot be read.

    An unreadable journal is one of the failures this path now reports — a turn refuses over it rather than proceeding on an empty history — and reading the latch through the same broken file would silence exactly that post. Answering *nothing was said* instead means the failure is reported every interval for as long as the journal stays unreadable, since the latch cannot be written either; between a repeated post and a silent stopped run, the repeat is the honest direction (workflow.md §9.2).
    """
    try:
        return read_notified(read_journal(workspace.journal).events)
    except OSError:
        return set()


def turn_post(result: "TendResult") -> Post | None:
    """The one post this turn earns, or `None` where it has nothing new to say.

    Args:
        result: The turn that just ran.

    Returns:
        A post, or `None`. Pure — resolves no channel and sends nothing, so the whole trigger vocabulary is testable without a notifier.
    """
    arriving = set(result.appeared)
    unattended = _unattended(result)
    newly = _newly_unattended(result)
    shown = [
        item
        for item in result.items
        if _reaches(item, arriving, unattended=unattended, newly=newly)
    ]
    # over every open item rather than the shown ones, which keeps a `clear`
    # from firing on a turn where the agent merely disposed of its own. A human
    # queue that empties while the agent's stays busy therefore hears nothing
    # until the next post — under-firing the one kind that is good news, which
    # is the cheap direction to be wrong in
    cleared = bool(result.resolved) and not result.items

    if (kind := _kind(result, shown, cleared=cleared)) is None:
        return None
    return Post(
        kind=kind,
        glyph=result.verdict.value,
        title=_title(result, shown),
        lines=_lines(result, _named(kind, shown)),
        table=_table(result.progress, WIDTH),
        narrow=_table(result.progress, NARROW),
    )


def _reaches(item: Item, arriving: set[str], *, unattended: bool, newly: bool) -> bool:
    """Whether one item is something to wake a person for, this turn.

    A human item reaches them when it is new, which is the whole of the edge policy: the id changes when the condition materially does, so one post per condition follows for free.

    **An agent item is different, and the difference had a hole in it.** It reaches them only where nobody is picking it up (`_unattended`) — except the self-healing ones, which Steward's own respawn is already resolving. But *newness* and *nobody is picking it up* are two edges that need not coincide: an item that appeared at 11pm to an attending agent is filtered out and spent, so when the agent goes quiet at 2am it is in no later diff and is escalated never. The escalation only ever caught items that happened to arrive after the agent had already gone.

    So the transition itself is an edge, and on that one turn every open agent item is offered — the same *once per condition* policy the ids give everything else, arrived at from the other side. What re-arms it is the workspace becoming unattended rather than the item becoming new.
    """
    if item.owner is not Owner.AGENT:
        return item.id in arriving
    if not unattended or self_healing(item):
        return False
    return newly or item.id in arriving


def _named(kind: Kind, shown: list[Item]) -> list[Item]:
    """The items worth spelling out under a title of this kind.

    **A `gate` names none of them, because its title already is the item.** `verdict()` returns 🏁 exactly when every open item is `signoff_ready`, and the line `verdict_line` writes for that verdict — *complete, the results are waiting to be accepted* — is that item's own sentence. Repeating it as a bullet adds the task count and nothing else, which the table's totals line carries anyway. What the gate post does still say is what *changed* to produce it: the tasks that finished this turn, and anything that closed.

    Every other kind names them, because there the title is a count and the items are what it counted.
    """
    return [] if kind is Kind.GATE else shown


def _unattended(result: "TendResult") -> bool:
    """Whether the agent's items have nobody to pick them up.

    Three answers from two facts the journal already holds, and they are not the same question. **Never collected** is not an agent that has gone quiet — it is a workspace no agent has ever been attached to, which needs no horizon to be sure of. A collection whose age cannot be read is history rather than damage, and an unreadable stamp is not evidence that anybody left. Everything else is arithmetic against the cadence.
    """
    if result.collected is None:
        return True
    if result.since_collected is None:
        return False
    return result.since_collected >= UNATTENDED_INTERVALS * _cadence(result)


def held_tasks(
    result: "TendResult", *, spent: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Tasks whose completion is waiting on an agent's scan investigation.

    **A landed task with a scan finding is not one thing to say, it is two — so it says neither yet.** Posting *finished cybench* while an untriaged flag sits behind it tells the reader the wrong thing for as long as it stands alone, and the correction arrives as a second message about the same moment. Held, the run says one thing once: either the task finished, or it finished and here is what needs deciding.

    Four conditions, and each is doing distinct work. **An agent must be attached**, because a hold with nobody to release it is silence; with no agent the finish posts immediately and the finding escalates on `_unattended`'s own horizon two turns later, which is the right shape for that case. **A `scan:` window must still be open**, so a ruling releases the hold by itself and no separate signal is needed. **The completion must not already be spent**, because there is nothing left to defer once it has been announced. And **it must have been waiting for less than `HOLD_TENDS`**, so an agent that stopped answering cannot hold a completion all night.

    **The clock is the task's own log, not the window's opening.** A `scan:` class is run-wide by design — one reward-hacking decision covers the sweep — so a window opened by the first task to be flagged goes on absorbing the tenth task's findings hours later without its `opened_ts` moving. Measured against that, a task that landed a minute ago is already past the horizon and posts unheld, which is the one case the hold exists for. The log's `mtime` is when the run's own observation says the file last changed, which for a landed attempt is when it landed; where the filesystem does not date it, the window's opening stands in, and for the first task in a class the two are the same instant anyway.

    **Already-spent completions are excluded rather than re-held**, and it is the difference between deferring an announcement and retracting one. The set returned here is subtracted from the completion baseline the next turn diffs against, so holding a task whose finish was announced turns ago removes it from that baseline and the next turn announces it a second time. A late finding on an old completion is still a finding — it opens its window and reaches the agent as an item — but the finish it arrived after is not news twice.

    Args:
        result: The turn that just ran.
        spent: Completions already recorded, which is the baseline the diff is taken against (`_History.complete`).

    Returns:
        Task identifiers to withhold this turn — from the post *and* from the completion set the turn records, or the hold spends the diff instead of deferring it (`_turn`).
    """
    if _unattended(result):
        return frozenset()
    horizon = HOLD_TENDS * _cadence(result)
    landed = _landed(result)
    held: set[str] = set()
    for anomaly in result.anomalies.open:
        if anomaly.kind != "scan":
            continue
        opened = seconds_since(anomaly.opened_ts)
        for task in anomaly.evidence.tasks:
            if task in spent:
                continue
            waiting = landed.get(task, opened)
            if waiting is None or waiting >= horizon:
                continue
            held.add(task)
    return frozenset(held)


def _landed(result: "TendResult") -> dict[str, float]:
    """Per task, how long ago the observation says its current attempt's log last changed.

    In seconds, from the `mtime` the listing reports in milliseconds. Tasks whose attempt carries no `mtime` are absent rather than zero, so the caller falls back to the window rather than treating an undated log as one that landed this instant.
    """
    if result.observed is None:
        return {}
    now = time.time() * 1000
    return {
        task.identifier: (now - task.current.mtime) / 1000
        for task in result.observed.tasks
        if task.current is not None and task.current.mtime is not None
    }


def _newly_unattended(result: "TendResult") -> bool:
    """Whether *this* is the turn on which the agent's items became the person's.

    **Stateless, and it can be**, which is what keeps this module's *no latches of its own* property: `_unattended` is a threshold on the collection age, so the turn that crossed it is the one whose age passed the horizon somewhere inside the gap it is answering for. Nothing has to be remembered about the previous turn.

    **The gap is the real one, not the nominal one**, and that is what makes this survive a missed tend. Against a fixed cadence a timer that skipped a turn — or fired late — puts the age past `horizon + cadence` on the next run, and the handoff is lost silently for the whole of that run: the one item nobody picked up becomes the one item nobody is told about. `Supervision.since_tend` is how long it has actually been since the previous recorded turn, which is exactly the interval this turn is responsible for, so a long gap widens the window it covers by precisely as much as it delayed the crossing. It falls back to the cadence only where there is no previous turn to measure against.

    Two answers are deliberately `False`. A workspace **nobody has ever collected** is unattended from its first turn, so every item is new when it is offered and there is no transition to catch. And an age that cannot be read is history rather than damage, on `_unattended`'s own reasoning.
    """
    if result.collected is None or result.since_collected is None:
        return False
    horizon = UNATTENDED_INTERVALS * _cadence(result)
    return horizon <= result.since_collected < horizon + _gap(result)


def _gap(result: "TendResult") -> float:
    """How long it has been since the previous recorded turn — the span this one answers for.

    The cadence stands in where there is no previous turn, and where the record puts two turns at the same instant: a zero-width span would answer for nothing at all, which is the one reading that can never be right.
    """
    since = result.supervision.since_tend if result.supervision is not None else None
    return since if since else _cadence(result)


def _cadence(result: "TendResult") -> int:
    """Seconds between tends, as the timer actually installed them.

    The *armed* interval rather than the one `_steward.yaml` expresses, because the question is how much real time two turns take and only the arming answers that — `Supervision.interval` is the expressed preference, kept for comparing against what is armed. A workspace with no timer is being tended by hand, where the default is as good a guess as any and the reader is at the terminal anyway.
    """
    armed = result.supervision.armed if result.supervision is not None else None
    return armed.interval if armed is not None else DEFAULT_TEND_INTERVAL


def _kind(result: "TendResult", shown: list[Item], *, cleared: bool) -> Kind | None:
    """Why this post is being sent, of however many reasons it has.

    Precedence rather than one post per reason, and the order is by what a reader would want to have been told if they only read the first line. A new decision outranks a finished task; the run reaching its gate outranks both, because it is the one that ends the night.
    """
    # **a signed run says nothing, including *clear*.** `signoff` sends the one
    # terminal message itself and then takes the timer down, so anything a turn
    # posted afterwards would be a cheerful footnote to an ending — and the
    # ordinary case is exactly that: the signature closes the readiness item, so
    # the next turn's diff would fire `clear` on a run that is over
    if result.verdict is Verdict.SIGNED_OFF:
        return None
    if shown:
        if result.verdict is Verdict.COMPLETE:
            return Kind.GATE
        if result.verdict is Verdict.STOPPED:
            return Kind.STOPPED
        return Kind.ATTENTION
    if cleared:
        return Kind.CLEAR
    if result.finished:
        return Kind.PROGRESS
    return None


def _title(result: "TendResult", shown: list[Item]) -> str:
    """The verdict as one line, counting only what this reader has to act on.

    **One undifferentiated count, and the one place the wording leaves `verdict_line` behind.** That function splits *needs a person* from *for the agent* because its readers — `status.md`, the terminal — include the agent, for whom the split is the routing. Here there is one reader and everything in front of them is theirs by construction: an item that is only here because nobody collected is a person's job in exactly the way a `stalled` task is, and asking them to sort the two would be exporting Steward's bookkeeping to the person it is supposed to spare.

    **Counted in decisions, which is the one noun that is true of all of them.** *Tasks* would be wrong about half the vocabulary — `drift`, `degraded`, `unsupervised`, `timer_drift` and `signoff_ready` are facts about the run with no task behind them — and a bare count is a sentence with a hole in it. `decisions` is also what the body already calls them where it runs out of room (`_capped`), so the title and the line under it are not two words for one thing.

    Delegates every other case, so the pause, the gate and *nothing needs you* cannot come to be spelled two ways. No glyph: it is `Post.glyph`, which the heading puts in front of the workspace name rather than in front of the sentence.
    """
    if not shown or result.verdict in (
        Verdict.PAUSED,
        Verdict.CLEAR,
        Verdict.COMPLETE,
        Verdict.SIGNED_OFF,
    ):
        return verdict_text(result.verdict, shown)
    one = len(shown) == 1
    needs = (
        f"{len(shown)} decision{'' if one else 's'} need{'s' if one else ''} attention"
    )
    if result.verdict is Verdict.STOPPED:
        return f"nothing is progressing, {needs}"
    return needs


def _lines(result: "TendResult", shown: list[Item]) -> list[str]:
    """Everything that changed, in reading order and free of markup.

    All of it, whatever the kind — the kind says which of these a reader is being woken for, not which of them happened.

    **Nothing says an item was routed here because no agent picked it up.** The escalation decides *whether* to show it (see `_unattended`); once it is shown, the reader's question is what the item says, and a line explaining the routing is one they can act on nothing with.
    """
    short = _keys(result.progress)
    lines = _capped([_item(item, short) for item in shown], "decisions")
    named = _named_tasks(result.progress, short)
    finished = [
        f"finished {named[identifier]}"
        for identifier in result.finished
        if identifier in named
    ]
    return lines + _capped(finished, "tasks") + _scan_note(result)


def _scan_note(result: "TendResult") -> list[str]:
    """One line where a task being reported finished still carries an unruled scan flag.

    **The summary line, and nothing per task.** A finish that reached this post either was never held or has run out of hold (`held_tasks`), and in the second case the reader is being told a task is done while a decision about its results is still outstanding — which is one fact about the post, not a qualifier on each row. The windows themselves are items, and say which classes and how many; repeating that here would put the same finding in a phone message twice.
    """
    flagged = {
        task
        for anomaly in result.anomalies.open
        if anomaly.kind == "scan"
        for task in anomaly.evidence.tasks
    }
    named = flagged & set(result.finished)
    if not named:
        return []
    return [f"{len(named)} with scan findings nobody has ruled on"]


def _keys(progress: Progress) -> dict[str, str]:
    """Each task's display key, mapped to the shortest form this post can use.

    **Which reverses `items.py`'s rule that a summary names its task in full, and only here.** That rule is about an item *travelling alone* — into a title, into a line with no table under it — where `sec_bench_pro` is enough to pick a row out of five and only the full key names a task to somebody who cannot see the other four. A post is the case where they can: the table is directly beneath, and where every row shares a model the totals line says it outright. So the part being elided is on screen either way, and spelling `@anthropic/claude-opus-5` after every task name costs a phone reader a line each time to say what the line below already said.

    Only keys that actually shorten are in the mapping, so a run whose tasks disagree about their models keeps every one of them.
    """
    short = short_keys(progress.rows)
    return {
        row.key: name
        for row, name in zip(progress.rows, short.keys, strict=True)
        if name != row.key
    }


def _named_tasks(progress: Progress, short: dict[str, str]) -> dict[str, str]:
    """Each task's identifier, mapped to what a reader should be shown instead.

    `TendResult.finished` is identifiers, because it is diffed against what an earlier turn recorded and a display key moves when the definition changes around it. Nobody wants to read one: this is where it turns back into `cybench`.

    A task the current definition no longer asks for has no row and so no name, and is left out rather than shown as a digest — it is a task that finished under a definition this run has replaced, which is not this run's news.
    """
    return {
        row.identifier: short.get(row.key, row.key)
        for row in progress.rows
        if row.identifier
    }


def _shortened(line: str, short: dict[str, str]) -> str:
    """The line with each task named as the table beneath it names them.

    Longest first, so a key that is a prefix of another does not claim its text.
    """
    for full, name in sorted(short.items(), key=lambda pair: -len(pair[0])):
        line = line.replace(full, name)
    return line


def _item(item: Item, short: dict[str, str]) -> str:
    """One new decision, said the way its owner would ask about it.

    Unmarked by owner, because by the time a line is here the distinction has already been made: the agent's items reach this list only where there is no agent, and tagging one *for the agent* in front of the person who has to go and start one would be naming the routing rather than the job.

    **And carrying a command only where a person is the one who runs it.** `steward launch`, `steward timer arm` and `steward timer status` are right on the item — `status.md` is read by the agent, and they are exactly what it should do — but printing one into a channel invites the reader to drive Steward by hand, when the arrangement everywhere else is that they say what they want and the agent does it. The kinds whose action survives are the ones in `FIXED_OWNER`: a kind whose owner policy may never move to the agent is, for the same reason, one whose action no agent can perform. Today that is the park, whose command reaches the worker holding a sample hostage — the one thing in the whole vocabulary that a person and only a person can end.

    Where a command does travel it travels whole, trimmed summary or not: a command is either runnable or worthless, where half a sentence still says most of what the sentence said.
    """
    said = _trimmed(_shortened(item.summary, short))
    if item.action is None or item.kind not in FIXED_OWNER:
        return said
    return f"{said} ({item.action})"


def _trimmed(said: str) -> str:
    """An item's own words, at a length somebody reads on a phone.

    Cut at a word rather than mid-token, and marked, for the reason `_capped` marks a shortened list: a truncation nothing declares reads as the whole of it, and a log filename that has quietly lost its tail is worse than one that says it was cut.
    """
    if len(said) <= SAID:
        return said
    return f"{said[:SAID].rsplit(' ', 1)[0].rstrip(' ,;:(—-')}…"


def _capped(lines: list[str], noun: str) -> list[str]:
    """At most `LINES` of them, with what was left out named rather than dropped.

    The discipline `status.md` already keeps: a shortened list with nothing saying so reads as the whole of it, which for a notification is the difference between *two tasks finished* and *you were not told about the other forty*.
    """
    if len(lines) <= LINES:
        return lines
    return [*lines[:LINES], f"and {len(lines) - LINES} more {noun}"]


def _table(progress: Progress, width: int) -> list[str]:
    """The progress table, shortened to something a phone can hold.

    The middle of a long run is replaced by its own count while the shared model stays, because the model describes every task whether or not its row is here.
    """
    rows = progress_table(progress, width=width)[: len(progress.rows)]
    tail = [] if (shared := _shared(progress)) is None else [shared]
    if len(rows) <= ROWS:
        return rows + tail
    return [*rows[:ROWS], f"... {len(rows) - ROWS} more tasks", *tail]


def _shared(progress: Progress) -> str | None:
    """The line under the table, carrying the one thing the keys dropped.

    **No totals.** `progress_table`'s own footer sums every live column, and in a post that is a line of churn: samples, running and queued are columns in the rows above, so totalling them restates the screen with a number that is different every ten minutes, under a table the reader has just read. A post is read once, by somebody deciding whether to get up.

    What is left is the model where the keys elided it, for the reason the terminal keeps it: a table that shows the model nowhere has lost it.
    """
    model = short_keys(progress.rows).model
    return None if model is None else f"  {model}"


__all__ = [
    "HOLD_TENDS",
    "LINES",
    "ROWS",
    "SAID",
    "UNATTENDED_INTERVALS",
    "held_tasks",
    "notify_failure",
    "notify_turn",
    "turn_post",
]
