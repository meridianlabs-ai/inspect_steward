"""Getting a post out, without ever letting that cost a turn.

**Steward calls Apprise itself rather than `inspect_ai.util.notify()`**, and the reason is the dialect. That function passes no `body_format`, so every target is handed the raw body whatever it declared; it also resolves its instance from a `ContextVar` installed inside an eval, and a tend is not inside one. What is borrowed from upstream is `build_apprise` — the discipline that keeps URLs out of arguments — and nothing else, which is ten lines rather than a dependency on eval scope.

**Never raises, and bounded.** A post is the last thing a turn does and the least important: an eval must not fail because Slack was slow. Five seconds matches the cap upstream chose, and it is shorter than any plausible human reaction time, so nothing waits on it noticeably. Failures land in `steward.log` — the trail across turns, which is where a channel that has been broken since Tuesday reads as one fact rather than as forty unrelated bad nights.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anyio
import anyio.to_thread
import apprise

from .._workspace import steward_log
from .dialect import by_dialect
from .post import Post
from .render import body_format, render

SEND_TIMEOUT = 5.0
"""Seconds a post may take before it is abandoned.

The cap upstream chose for the same call, and kept rather than re-derived: a caller here is finishing a turn, and a turn that waits on a notifier is a turn that stopped supervising a fleet.
"""


@dataclass(frozen=True)
class Delivery:
    """What became of one post.

    **Two numbers rather than one, because a channel is a list.** An operator posting to Slack and to a mailing list has two destinations that fail independently, and *some of it failed* and *none of it went* are different facts about the run: the first is a broken second target, which belongs in `steward.log`; the second is a reader who was not told, which is the only one worth acting on (`_tend.notify._owed`).
    """

    landed: int = 0
    """Groups of targets that accepted it, one group per dialect."""

    failures: list[str] = field(default_factory=list[str])
    """What could not be sent, by dialect, with the reason. Already recorded in `steward.log`. Empty where everything landed."""

    @property
    def reached_nobody(self) -> bool:
        """Whether this post is still owed to somebody — nothing at all got out."""
        return self.landed == 0


def send_post(instance: Any, post: Post, log: Path) -> Delivery:
    """Post to every target, each in the dialect it understands.

    **One send per dialect rather than one body for all of them.** Targets that disagree are partitioned (`dialect.by_dialect`), so a Slack reader is not charged for a mail client that was never going to see the formatting anyway.

    Never raises.

    Args:
        instance: The Apprise instance, from `channel_apprise()`.
        post: What to say.
        log: `steward.log`, where a failure is recorded.

    Returns:
        How much of it landed, and what did not.
    """
    landed = 0
    failures: list[str] = []
    try:
        servers = list(instance.servers)
    except Exception as ex:
        return Delivery(
            failures=[_failed(log, f"could not read the notification targets: {ex!r}")]
        )

    if not servers:
        # **an empty instance is a failure, not a quiet success.** `build_apprise`
        # answers with one for a URL Apprise cannot parse and for a config file
        # that names nothing, so treating it as *nothing to do* would report a
        # misconfigured channel as delivered — and latch a failure notification
        # that reached nobody (`_tend.notify.notify_failure`)
        return Delivery(
            failures=[
                _failed(
                    log,
                    "the notification channel resolved to no usable targets — check "
                    "the URL or config file it names",
                )
            ]
        )

    for dialect, group in by_dialect(servers).items():
        try:
            targeted = _instance(group)
            if targeted is None:
                continue
            refused = anyio.run(
                _dispatch,
                targeted,
                post.heading,
                render(post, dialect),
                body_format(dialect),
            )
        except Exception as ex:
            refused = repr(ex)
        if refused is None:
            landed += 1
        else:
            failures.append(
                _failed(log, f"could not post to the {dialect} channel: {refused}")
            )
    return Delivery(landed=landed, failures=failures)


async def _dispatch(instance: Any, title: str, body: str, format: str) -> str | None:
    """One bounded, best-effort dispatch.

    Apprise's API is synchronous, so it goes to a worker thread — `abandon_on_cancel` because a notifier that has stopped answering must not hold the turn open past the deadline it already missed.

    **The return value is the failure, not an exception**, and reading it is the difference between a channel that has been broken since Tuesday being reported and being invisible. Apprise catches a plugin's own exception, logs it to its logger, and answers `False`; a caller that only guarded against a raise would record every dead channel as delivered.

    Returns:
        Why the post did not land, or `None` where it did.
    """
    with anyio.move_on_after(SEND_TIMEOUT):
        delivered = await anyio.to_thread.run_sync(
            lambda: instance.notify(body=body, title=title, body_format=format),
            abandon_on_cancel=True,
        )
        return None if delivered else "the notifier would not accept it"
    return f"it did not answer within {SEND_TIMEOUT:.0f}s"


def _instance(servers: list[Any]) -> Any | None:
    """A fresh Apprise carrying just these targets.

    Apprise addresses a subset by tag, and the instance here was built by `build_apprise` with no tags — so re-adding the plugin objects to an empty instance is the way to speak to a group without asking upstream to tag on Steward's behalf.
    """
    grouped = apprise.Apprise()
    for server in servers:
        cast(Any, grouped).add(server)
    return grouped if len(grouped) else None


def _failed(log: Path, message: str) -> str:
    steward_log(log, message)
    return message


__all__ = ["SEND_TIMEOUT", "Delivery", "send_post"]
