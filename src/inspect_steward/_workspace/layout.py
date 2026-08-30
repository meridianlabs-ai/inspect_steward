from dataclasses import dataclass
from pathlib import Path

from .._evalset.detect import DefinitionType

JOURNAL = "journal.jsonl"
"""The durable record, and the file that marks a directory as a workspace."""

DEFINITION_NAMES: dict[DefinitionType, str] = {
    "evalset": "evalset.py",
    "flow": "flow.yaml",
    "hawk": "hawk.yaml",
}
"""Conventional definition filename per type. The definition pointer is discovered by name rather than configured, so these are the names looked for."""

GITIGNORE_ENTRIES = (
    ".steward/",
    "logs/",
    "logs-archive/",
    ".env",
)
"""Paths a workspace never commits. `logs/` and `logs-archive/` hold `.eval` archives, which are large outputs shared through an object store rather than through git; `.steward/` is disposable by construction. Ignored is not the same category as disposable — only `.steward/` is safe to delete.

`.env` is here because arming a timer tells people to write one: a scheduled tend runs under a stripped environment, and the answer Steward gives is *put the credentials in a file the workers already read* (`_timer.env`). Suggesting that without ignoring the file would be handing somebody a way to commit their API keys.

**Four entries rather than six**, since `steward.log` and `timer.log` moved inside `.steward/` and are covered by the line that was already here. A workspace created before that keeps two stale entries, because `ensure_gitignore` only ever appends — harmless, and cheaper than a rule for removing lines out of a file Steward does not own."""


@dataclass(frozen=True)
class Workspace:
    """A Steward workspace: the directory a human and an agent co-inhabit.

    Every path in the layout is derived here so that no other module builds one from a string. See workflow.md *`steward init` — the deliverable is a directory* for what each file is for, and *Three categories, and the one that matters* for which of them can be recovered if lost.
    """

    root: Path
    """Workspace root (absolute)."""

    # authored — the human's own work, and never overwritten
    @property
    def agents(self) -> Path:
        """`AGENTS.md` — the bootstrap, and the only thing discovered by convention."""
        return self.root / "AGENTS.md"

    @property
    def claude(self) -> Path:
        """`CLAUDE.md` — a symlink to `AGENTS.md` where the platform allows one."""
        return self.root / "CLAUDE.md"

    @property
    def directives(self) -> Path:
        """`_steward.yaml` — this human's standing rules, structured and not.

        Settings Steward acts on unattended, and a `policies` key an agent interprets when it arrives. Steward proposes changes and never writes it (workflow.md, *The one file Steward must never write*) — a rule under more pressure now the whole file is machine-shaped, and no less binding for it. The underscore sorts it to the top of a listing, beside `AGENTS.md`.
        """
        return self.root / "_steward.yaml"

    # durable machine state — nothing here can be rebuilt
    @property
    def journal(self) -> Path:
        """`journal.jsonl` — the append-only record of what was observed and decided."""
        return self.root / JOURNAL

    @property
    def logs(self) -> Path:
        """`logs/` — the flat log directory holding the current definition's results."""
        return self.root / "logs"

    @property
    def logs_archive(self) -> Path:
        """`logs-archive/` — superseded, removed, and failed logs. A sibling of `logs/` rather than a child, because log discovery recurses."""
        return self.root / "logs-archive"

    # disposable — rebuilt on the next tend, or simply gone with no loss
    @property
    def state(self) -> Path:
        """`.steward/` — claim, manifest, in-flight records, caches."""
        return self.root / ".steward"

    @property
    def claim(self) -> Path:
        """`.steward/claim` — the lock a writing command holds for as long as it runs.

        In the workspace rather than in `log_dir`, which two workspaces could share: `log_dir` is frequently S3, where the atomic primitive this needs is not reliably available. So the claim covers one workspace on one machine, the same boundary as the in-flight record.
        """
        return self.state / "claim"

    @property
    def manifest(self) -> Path:
        """`.steward/manifest.json` — desired state, as the last launch committed it.

        Disposable because it is derived: the definition is the source of truth and re-capturing rebuilds this exactly (configuration.md, *The manifest*). What is lost with it is not information but time — minutes for a Hawk config — and the knowledge of *which* capture the running fleet was converging toward, which is why `launch` commits it rather than every tend re-reading the definition (workflow.md, *One trigger, and one gate on it*).
        """
        return self.state / "manifest.json"

    @property
    def observed(self) -> Path:
        """`.steward/observed.json` — headers a turn does not have to read again.

        Purely an accelerator, and the most disposable thing in the workspace: every entry is a fact about a log file that is still sitting in the log directory. Losing it costs one slow turn, which is why nothing that touches it raises.
        """
        return self.state / "observed.json"

    @property
    def synced(self) -> Path:
        """`.steward/synced.json` — what the propagation last wrote out, by name and stamp.

        The most disposable thing here after the attempt cache, and for the same reason: every entry is a claim about a file that is still sitting in the workspace. Losing it costs one full propagation — which is why nothing that touches it raises, and why a record that will not parse is treated as one that does not exist.
        """
        return self.state / "synced.json"

    @property
    def workers(self) -> Path:
        """`.steward/workers/` — one selection document and one output file per spawned worker."""
        return self.state / "workers"

    @property
    def inflight(self) -> Path:
        """`.steward/inflight.jsonl` — what was spawned, appended before each launch.

        The journal's opposite in every way that matters: machine-only, and rebuildable from the process table on the next resolve, which is why it lives under `.steward/` (execution.md, *Detachment and the in-flight record*).
        """
        return self.state / "inflight.jsonl"

    @property
    def env(self) -> Path:
        """`.env` — credentials a scheduled tend and its workers both read.

        Not written by Steward and not required to exist. Named here because arming checks it (`_timer.env`) and because inspect loads it for free: `find_dotenv(usecwd=True)` searches up from a worker's cwd, which is this directory.
        """
        return self.root / ".env"

    @property
    def status(self) -> Path:
        """`status.md` — rewritten by every tend."""
        return self.root / "status.md"

    @property
    def log(self) -> Path:
        """`.steward/steward.log` — whether the machinery worked, as opposed to what it found.

        Disposable, and here rather than at the root because that is the category it belongs to. It used to sit at the top level for one stated reason — so the sync would carry it out without an exception to its own deny list — which is Steward dodging a rule Steward wrote. The sync names it instead, and the root is left for what a person authored and what a person reads.
        """
        return self.state / "steward.log"

    @property
    def timer_log(self) -> Path:
        """`.steward/timer.log` — what a scheduled tend printed, before Steward could log anything.

        **The constraint here is real and is met by the command rather than by the location.** Every backend used to open this path *before* starting Python — cron through a shell redirect, launchd through `StandardOutPath`, systemd through `StandardOutput=append:` — and none of the three creates a missing parent directory, so a path under `.steward/` turned a directory documented as safe to delete into one that silently stopped supervision forever. All three now run `mkdir -p` and their own redirect in one shell command (`_timer.entry.shell_command`), so deleting `.steward/` costs the previous contents of this file and nothing else: the next fire recreates the directory.

        Ordinary tends write nothing interesting here; `steward.log` remains where Steward says whether its own machinery worked. What lands here is the failure that happens too early to be logged anywhere else.
        """
        return self.state / "timer.log"

    def definition(self, type: DefinitionType) -> Path:
        """Conventional path for a definition of `type`.

        Args:
            type: Definition type.

        Returns:
            Path to the definition file (which need not exist).
        """
        return self.root / DEFINITION_NAMES[type]

    def find_definition(self) -> Path | None:
        """Locate this workspace's definition by name.

        Deliberately by filename rather than by `detect_definition_type`, which reads the file: an empty definition placeholder validates as both a flow spec and a hawk config, and would be reported ambiguous. Content-based detection happens later, when there is content.

        Returns:
            The definition, or `None` when the workspace has none yet. When several exist, the first in `DEFINITION_NAMES` order wins.
        """
        return next(
            (
                path
                for name in DEFINITION_NAMES.values()
                if (path := self.root / name).exists()
            ),
            None,
        )

    @classmethod
    def at(cls, root: Path | str) -> "Workspace":
        """Workspace rooted at a directory, whether or not it exists yet.

        Args:
            root: Workspace root.

        Returns:
            The workspace.
        """
        return cls(root=Path(root).resolve())

    @classmethod
    def find(cls, start: Path | str | None = None) -> "Workspace | None":
        """Locate the workspace containing a directory, searching upward.

        A workspace is identified by its journal: it is written at `init` and is the one file that cannot be rebuilt, so it is present for exactly as long as the workspace is.

        Args:
            start: Directory to search from (defaults to the current directory).

        Returns:
            The enclosing workspace, or `None` if there is none.
        """
        directory = Path(start or Path.cwd()).resolve()
        for candidate in (directory, *directory.parents):
            if (candidate / JOURNAL).exists():
                return cls(root=candidate)
        return None
