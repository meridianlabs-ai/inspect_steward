"""What a post is: a reason, a title, and a body somebody can act on.

**Kind and verdict are two axes, and conflating them is what made `complete` mean two things.** A kind is the post's *reason* — it decides how the post presents and who may send it. The verdict is the *run's state*, computed every turn, and it rides in the body of every post regardless. They are related and not the same: a `PROGRESS` post fires while the verdict is `CLEAR`, which is precisely the case a one-axis vocabulary cannot express.

**Four of the six are Steward's alone**, because each is either latched, terminal, or read off state the agent does not own. `ATTENTION` and `STOPPED` carry judgement, which is what makes them the agent's — and `steward notify` is the command that carries it (workflow.md §11.1).
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    """Why a post was sent."""

    ATTENTION = "attention"
    """Something worth knowing, and work continues. The agent's, and Steward's when an item appears at that level."""

    STOPPED = "stopped"
    """Nothing progresses until a person answers. The agent's, and Steward's for a parked worker — the one `stopped` it sends without an agent present, because an absent agent plus a parked worker is the silence-while-stalled the timer exists to prevent."""

    PROGRESS = "progress"
    """Tasks finished this turn. Steward's alone, and **batched by the tend**: a turn that finishes five tasks posts once naming five, because the tend is already the clock and a post per task is the noise-fatigue failure workflow.md §11 opens with."""

    CLEAR = "clear"
    """The decision queue emptied and work continues. Steward's alone."""

    GATE = "gate"
    """Every task finished; the run is waiting on `signoff`. Steward's alone and **latched** — posted by the first turn that finds the run settled, and re-armed by a later `launch`, which is the case a manual convention would get wrong.

    This is the post for `Verdict.COMPLETE`, whose 🏁 means *finished and unaccepted*. The two names differ on purpose: a verdict describes the run, a kind describes why a message was sent, and `signed_off` is the one that means accepted.
    """

    SIGNED_OFF = "signed_off"
    """The run was accepted. Steward's alone, terminal, sent once — and not yet sendable, since `signoff` is a later step. Named here so the vocabulary is complete and so nothing else claims the word."""


AGENT_KINDS = frozenset({Kind.ATTENTION, Kind.STOPPED})
"""What `steward notify` may send.

The rest are refused there rather than merely undocumented: a hand-sent `gate` is a claim about the run that nobody computed, and a hand-sent `signed_off` is a claim that a human adjudicated, which is the whole content of signoff.
"""

GLYPH = {Kind.ATTENTION: "⚠️", Kind.STOPPED: "🛑"}
"""The character a title leads with, for the posts whose title is written by hand.

**Only these two, and the reason is that a kind does not imply a glyph in general.** Steward's own posts take theirs from the verdict, which is a statement about the *run* — a `PROGRESS` post fires while the verdict is ✅ and can equally fire while it is ⚠️, so a table mapping `PROGRESS` to either would be wrong half the time. What is left is exactly the posts with no verdict behind them: the two kinds `steward notify` sends, and the one Steward sends about a turn that never got far enough to compute one.

The characters are `Verdict.ATTENTION` and `Verdict.STOPPED` deliberately. A reader scanning a channel is sorting on the first character, and a run that is stopped and an agent saying it is stopped are the same news arriving two ways.
"""


WIDTH = 76
"""Display-key width for the progress table.

The width `status.md` and the terminal already use.
"""

NARROW = 28
"""Display-key width for the Slack family, chosen for a phone rather than a laptop.

A wide monospace block side-scrolls on the device where a 3am post is actually read, and a reader who has to drag a code block sideways to find the task name reads the title and nothing else.
"""


@dataclass(frozen=True)
class Post:
    """One notification, before it is rendered for any particular target."""

    kind: Kind
    """Why this was sent."""

    title: str
    """The verdict line. Also the post's title, so the last message in a channel is true modulo what its reader has since answered."""

    lines: list[str] = field(default_factory=list[str])
    """What happened, one item or task per line, already in reading order and free of markup."""

    table: list[str] = field(default_factory=list[str])
    """The progress table, pre-aligned to `WIDTH`, to be rendered as a monospace block. Empty where there is nothing to show."""

    narrow: list[str] = field(default_factory=list[str])
    """The same table at `NARROW`, for the Slack family. Empty to use `table` for every dialect.

    **Two renderings carried rather than one narrowed on the way out**, because the rows arrive already padded to a common column width: trimming them afterwards cuts columns off the right-hand end rather than shortening the key on the left, which is the one part a reader is scanning for. Building both costs one more pass over rows a turn has already computed.
    """

    footer: str | None = None
    """Where the logs are, or `None`. The one thing a reader away from the machine cannot look up."""

    glyph: str | None = None
    """The character the title leads with, in front of the workspace name.

    **In front of the name rather than the sentence**, because those are two different scans. A reader with one channel and six runs finds the run by its name; a reader with one run finds the urgent message by its glyph — and the glyph is the coarser sort, so it goes first: `🛑 my-sweep: the tend could not run`. Carried rather than written into `title` so that both can be true without a renderer taking characters off the front of a string.
    """

    workspace: str | None = None
    """The workspace directory's name, which the target shows in front of the title.

    **One channel serves however many runs a person has going**, and every title here is a sentence about *a* run — *2 decisions need attention* names nothing, and two workspaces posting it an hour apart are indistinguishable. The directory name is what a person calls the run in conversation and in `cd`, so it is the name they will recognise; nothing else Steward holds is both stable and theirs.

    Carried rather than baked into `title` so that the callers assembling a title stay about the run, and one place decides how the two are joined.
    """

    @property
    def heading(self) -> str:
        """The title as a target shows it: the glyph, the run, then what happened."""
        named = (
            self.title if self.workspace is None else f"{self.workspace}: {self.title}"
        )
        return named if self.glyph is None else f"{self.glyph} {named}"

    def monospace(self, narrow: bool) -> list[str]:
        """The table to render, for a dialect that wants the narrow one or not."""
        return self.narrow if narrow and self.narrow else self.table


__all__ = ["AGENT_KINDS", "GLYPH", "NARROW", "WIDTH", "Kind", "Post"]
