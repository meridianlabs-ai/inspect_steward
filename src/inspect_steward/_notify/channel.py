"""Where Steward posts, and how the fleet comes to agree with it.

**One channel, two consumers.** Steward posts what a turn found; a worker posts what a sample asked — `ask_user()`, the operator approver — and those are opposite semantics on one pipe (workflow.md §11.4). What they must not be is two *destinations*, because the failure that produces is silent: a fleet notifying somewhere nobody reads, or not at all, while `status.md` says everything is fine.

**So the relationship is reflexive, and both directions are here.** Steward's own setting is exported as `INSPECT_EVAL_NOTIFICATION`, which `_worker.spawn` spreads into every worker; and where only inspect's variable is set, Steward posts to that. Either spelling configures both halves, which is the whole point of taking `notification` out of the override aliases (`_workspace.directives.STEWARDS`).

**Exporting is not the whole of reaching the fleet.** `build_apprise(True)` reads the variable, but a worker's `eval_set()` only calls it when its `notification` argument is truthy — so the value alone leaves the fleet silent, and the `notification` override in each worker's selection is the other half (`_worker.spawn`).

**And the resolution has to be reportable, because every spelling of it is invisible from outside this process.** The channel that reaches an operator most often comes from a `.env` at or above the workspace, which inspect loads into Steward and into nothing else — so a reader who checks `_steward.yaml` and their own shell finds no channel and is wrong. `describe_channel` is the answer to that, and `Channel` is what it may say: a name, a count, and no value.

**A scheduled tend inherits neither variable**, which is why the `_steward.yaml` key earns its place: it is the one spelling still there at 02:00. `_timer.env` refuses to arm when the arming shell holds a channel the workspace's `.env` does not, so the case is caught rather than discovered in the morning.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# reference-only notification config, deliberately not public API upstream and
# deliberately left that way: what is wanted here is the discipline it
# enforces, not a second copy of the URL parsing
from inspect_ai.util._notify import build_apprise

from .._workspace import Directives, Workspace, declared_notification

INSPECT_NOTIFICATION = "INSPECT_EVAL_NOTIFICATION"
"""Inspect's channel variable, which Steward both reads and writes."""

STEWARD_NOTIFICATION = "STEWARD_NOTIFICATION"
"""Steward's own channel variable, named here so a report can say where a channel came from."""

DIRECTIVES = "_steward.yaml"
"""The workspace file, as the third spelling names itself."""

COMMAND_LINE = "--notification"
"""The fourth spelling, which lasts exactly as long as the invocation that typed it."""


def establish_channel(
    workspace: Workspace,
    directives: Directives | None = None,
    *,
    notification: str | bool | None = None,
    fleet: str | bool | None = None,
) -> str | None:
    """Settle where this process and its workers post, and make the two agree.

    Called once by anything that spawns or posts, before it does either. Mutates `os.environ`, deliberately: that is the channel a worker inherits, and a return value nobody could inherit would leave the fleet configured by whatever the shell happened to hold.

    **Steward's setting overwrites inspect's variable rather than deferring to it.** The narrower spelling winning is the rule `read_overrides` already applies to every other pair of names, and here it is load-bearing rather than tidy: leaving a differing variable in place would have Steward post to one channel while its fleet posted to another, which is exactly the divergence this module exists to prevent. The two agreeing matters more than which of them was set first.

    **Declining silences Steward and never the fleet, which means it cannot be a `return` on the way in.** `--no-notification` overrides a channel the workspace named, and taking that as *there is no channel* would leave every worker this turn spawns unable to reach anybody — so a sample that stops on `ask_user()` or a tool approval holds its slot, its sandbox and its model connections until morning, with nobody told. That is the failure the whole rule exists to prevent, arriving through the option that promises the opposite. So the decline is applied to the *return value* and the workspace's own spelling is still exported.

    Args:
        workspace: The workspace, against which a config-file path is resolved.
        directives: What `_steward.yaml` and the environment said, or `None` where the file would not parse — a condition worth notifying about, and the reason this takes the absence rather than requiring the caller to have succeeded. `STEWARD_NOTIFICATION` is then read on its own (`declared_notification`).
        notification: A target from the command line, `False` for none, or `None`.
        fleet: What the workspace's own spellings name, for a caller whose `notification` is a command-line override that has already replaced them. Only read where Steward is declining, which is the one case where the two answers differ. `None` where the caller has no separate answer, and `directives` is asked instead.

    Returns:
        Where **Steward** posts, or `None` where it posts nowhere. A declining workspace returns `None` having exported the fleet's channel.
    """
    declared = notification if notification is not None else _declared(directives)
    if declared is False:
        _exported(workspace, fleet if fleet is not None else _declared(directives))
        return None
    return _exported(workspace, declared) or (
        os.environ.get(INSPECT_NOTIFICATION, "").strip() or None
    )


def _exported(workspace: Workspace, declared: str | bool | None) -> str | None:
    """Put a channel where every worker will inherit it, if there is one.

    A config-file *path* is made absolute first: a worker's cwd is the workspace, and nothing promises the tend's is.
    """
    if not isinstance(declared, str):
        return None
    target = _located(workspace, declared)
    os.environ[INSPECT_NOTIFICATION] = target
    return target


def _declared(directives: Directives | None) -> str | bool | None:
    """What Steward's own spellings said, whether or not the file could be read."""
    if directives is not None:
        return directives.notification
    return declared_notification(os.environ)


def channel_apprise() -> Any | None:
    """The Apprise instance for the settled channel, or `None` where it will not build.

    Takes no target and reads none: `build_apprise(True)` reads `INSPECT_NOTIFICATION`, which `establish_channel` has already set to the channel in force. That is the point — the discipline keeping URLs out of arguments is honoured without Steward parsing one itself, and there is no second copy of the value to disagree with the first.

    Never raises. A channel that will not build is a misconfiguration worth reporting, and a caller here is either finishing a turn or already handling a failure; neither may be lost to it.

    Returns:
        An `apprise.Apprise`, or `None` where the variable is unset or names something unusable.
    """
    try:
        return build_apprise(True)
    except Exception:
        return None


def usable_channel(workspace: Workspace, target: str) -> bool:
    """Whether this channel resolves to something Apprise can actually post to.

    **Answered by building it, because every cheaper answer is wrong.** The setting takes a URL, a comma-separated list of them, or a path to an Apprise config file, and each of the three has a way of being present and useless: a mistyped scheme parses to no plugin, a config file that has been moved or renamed reads as empty, and a config file with a typo in it yields a list of no targets. Apprise reports all three identically — an instance with nothing in it — and `send_post` already treats that as a failure. What is missing without this is that the failure first happens at 02:00, in the one channel whose whole job is to be the thing that tells you.

    **Against a restored environment**, because the caller is `launch` asking what a *scheduled* tend will find: settling the variable for real here is what makes a `--notification` flag good for one turn look durable.

    Args:
        workspace: The workspace, against which a config-file path is resolved.
        target: The channel to try, as a durable spelling named it.

    Returns:
        Whether it built, and built with at least one target in it.
    """
    before = os.environ.get(INSPECT_NOTIFICATION)
    os.environ[INSPECT_NOTIFICATION] = _located(workspace, target)
    try:
        instance = channel_apprise()
        return instance is not None and len(instance) > 0
    finally:
        if before is None:
            os.environ.pop(INSPECT_NOTIFICATION, None)
        else:
            os.environ[INSPECT_NOTIFICATION] = before


def _located(workspace: Workspace, target: str) -> str:
    """A config-file path made absolute; a URL left exactly as it is.

    A relative path would otherwise mean two directories: a worker's cwd is the workspace root, and nothing says the process that resolved this was standing there. The discriminator is `://`, which every Apprise URL has and no path does — the same test `resolve_log_dir` uses one module over.
    """
    if "://" in target:
        return target
    return str((workspace.root / target).resolve())


@dataclass(frozen=True)
class Channel:
    """Where this run's notifications go, said without ever naming the value.

    **The one setting whose absence and whose presence were both invisible.** A channel is configured in four places and read from a fifth: `notification` in `_steward.yaml`, `STEWARD_NOTIFICATION`, `INSPECT_EVAL_NOTIFICATION`, `--notification`, and — the one that produced this type — a `.env` at or above the workspace, which `init_dotenv()` loads into Steward's own process and into nobody else's. An agent that reads the file and the shell it was handed sees none of it, concludes the run cannot reach anybody, and asks an operator to fix a thing that is not broken. Nothing in the snapshot contradicted it, because the snapshot did not mention notification at all.

    So this is reported beside the log directory, on the same grounds: it stopped being guessable, and the two ways of guessing are both wrong. It is a **fact line and never an item** — the argument in `_cli.launch._echo_no_channel` still holds, that a remedy repeated every ten minutes is what teaches a reader to skip the channel it is advertising. Stating what is in force is not that; the sentence carries no advice, and the advice stays at launch where it is heard once.

    **Nothing here is the value.** The whole discipline the setting inherits from upstream is that an Apprise URL is a bearer token with a scheme in front of it, and this document is written to a file, printed to a terminal, and synced to an object store. A name, a count and two booleans answer *will anything reach me* without any of that.
    """

    source: str | None = None
    """Which spelling put a channel in front of Steward, or `None` where none did.

    One of `DIRECTIVES`, `STEWARD_NOTIFICATION`, `INSPECT_NOTIFICATION` or `COMMAND_LINE` — the name an operator would search for, rather than a category they would then have to translate. `None` while Steward is declining, which is `declined`'s to explain.
    """

    targets: int | None = None
    """How many places a post actually reaches, or `None` where there was nothing to build.

    **The count rather than a yes**, because a declaration is not a channel and the gap between them is silent: a mistyped scheme parses to no plugin, a moved config file reads as empty, and a bad edit leaves a config with no targets in it. All three are a non-empty setting that reaches nobody (`usable_channel`), and a report that said *configured* over any of them would be the reassurance without the capability.
    """

    declined: bool = False
    """Whether Steward was asked to post nowhere — `notification: false`, or `--no-notification`."""

    fleet: bool = False
    """Whether the workers this turn spawns inherit a channel.

    Not the same question as the rest of this type, and it comes apart from it in the case that matters: `--no-notification` silences Steward's own posts and must still leave the fleet able to reach somebody, because a worker's notification is a sample stopped on `ask_user()` or an approval and waiting (`establish_channel`). A declining run whose fleet is also silent is a different thing to read about at 2am than one whose fleet is not.
    """

    @property
    def reaches(self) -> bool:
        """Whether a post from Steward would arrive anywhere."""
        return bool(self.targets)

    @property
    def description(self) -> str:
        """One line, for the terminal and the markdown alike.

        **Facts, and deliberately no remedy.** `launch` says what to do about a missing channel, once, at the moment somebody is watching; repeating it in a document regenerated every ten minutes would be the nagging that trains a reader past it — and would print the same two sentences twice in the one place both fire.
        """
        if self.declined:
            return (
                "declined — Steward posts nowhere; the fleet still posts"
                if self.fleet
                else "declined — nothing posts anywhere"
            )
        if self.source is None:
            return "none configured — steward notify will fail"
        if not self.targets:
            return f"{self.source} · no usable targets"
        return (
            f"{self.source} · {self.targets} target{'' if self.targets == 1 else 's'}"
        )


def describe_channel(
    *,
    target: str | None,
    notification: str | bool | None,
    channel: str | bool | None,
    environ: Mapping[str, str] | None = None,
) -> Channel:
    """Classify the channel a turn has already settled, for whoever reads the snapshot.

    **It classifies and never resolves**, which is what keeps it from becoming a second answer to the question `establish_channel` answers: the settled target is an argument, so a report that disagreed with what Steward will actually do is not expressible. What it adds is the one thing the return value cannot carry — *which spelling this came from* — because a channel that lives in a `.env` above the workspace and a channel that lives in the committed file are the same string and very different facts.

    **Precedence is read rather than restated.** The environment beats the file because `read_directives` overlays it, so a `STEWARD_NOTIFICATION` in scope is the source whenever there is one; asking the variable directly is how that stays true if the overlay ever changes, rather than a copy of it that would not.

    Args:
        target: What `establish_channel` returned — where Steward posts, or `None`.
        notification: The channel Steward was asked for, flag included.
        channel: What the *workspace's* own spellings said, whatever a flag said. The two differ only where a flag was given, which is exactly how a flag is detected.
        environ: The environment to read, defaulting to this process's — which by this point holds whatever `establish_channel` exported, so the fleet's half is read from the same place a worker will read it.

    Returns:
        What is in force, with no value in it.
    """
    env = os.environ if environ is None else environ
    fleet = bool(env.get(INSPECT_NOTIFICATION, "").strip())
    if target is None:
        # `False` is an operator declining; `None` is nobody having said anything,
        # and the two want different sentences from a reader's point of view
        return Channel(declined=notification is False, fleet=fleet)
    return Channel(
        source=_source(notification, channel, env),
        targets=_targets(),
        fleet=fleet,
    )


def _source(
    notification: str | bool | None,
    channel: str | bool | None,
    environ: Mapping[str, str],
) -> str:
    """Which of the four spellings supplied the channel now in force."""
    if notification is not None and notification != channel:
        return COMMAND_LINE
    if environ.get(STEWARD_NOTIFICATION, "").strip():
        return STEWARD_NOTIFICATION
    if isinstance(channel, str):
        return DIRECTIVES
    return INSPECT_NOTIFICATION


def _targets() -> int:
    """How many places the settled channel resolves to, and never what they are."""
    instance = channel_apprise()
    return 0 if instance is None else len(instance)


__all__ = [
    "COMMAND_LINE",
    "DIRECTIVES",
    "INSPECT_NOTIFICATION",
    "STEWARD_NOTIFICATION",
    "Channel",
    "channel_apprise",
    "describe_channel",
    "establish_channel",
    "usable_channel",
]
