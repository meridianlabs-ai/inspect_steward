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

GITIGNORE_ENTRIES = (".steward/", "logs/", "logs-archive/")
"""Paths a workspace never commits. `logs/` and `logs-archive/` hold `.eval` archives, which are large outputs shared through a log store rather than through git; `.steward/` is disposable by construction. Ignored is not the same category as disposable — only `.steward/` is safe to delete."""


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
    def policy(self) -> Path:
        """`policy.md` — this human's standing rules. Steward proposes changes and never writes it."""
        return self.root / "policy.md"

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
    def status(self) -> Path:
        """`status.md` — rewritten by every tend."""
        return self.root / "status.md"

    @property
    def log(self) -> Path:
        """`steward.log` — whether the machinery worked, as opposed to what it found."""
        return self.root / "steward.log"

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
