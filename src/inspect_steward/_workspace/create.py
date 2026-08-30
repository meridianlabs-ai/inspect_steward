"""Creating a workspace, and never overwriting one.

`init` only ever creates. Every authored file in a workspace is someone's work — the definition and `_steward.yaml` most obviously, but `AGENTS.md` too once anyone has adjusted it — so a second run reports what it found rather than restoring a pristine copy over it. The one exception is `.gitignore`, which is appended to, because its entries are Steward's rather than the author's.
"""

import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from importlib import resources
from pathlib import Path

from .._evalset.detect import DefinitionType
from .journal import append_event
from .layout import GITIGNORE_ENTRIES, Workspace


class Outcome(str, Enum):
    """What happened to one path during `init`."""

    CREATED = "created"
    KEPT = "kept"
    UPDATED = "updated"
    """Only `.gitignore`, which gains missing entries rather than being replaced."""

    SKIPPED = "skipped"
    """Wanted but not possible here, with a reason — a missing git, or symlinks the platform will not make."""


@dataclass(frozen=True)
class Step:
    """One path `init` considered."""

    path: str
    """Path relative to the workspace root, or a bare name for an action like `git init`."""

    outcome: Outcome
    detail: str = ""
    """Why, when that is not obvious from the outcome."""


@dataclass
class CreateReport:
    """What `init` did, in the order it did it."""

    workspace: Workspace
    steps: list[Step] = field(default_factory=list[Step])

    @property
    def created_anything(self) -> bool:
        return any(step.outcome is Outcome.CREATED for step in self.steps)

    def record(self, path: str, outcome: Outcome, detail: str = "") -> None:
        self.steps.append(Step(path=path, outcome=outcome, detail=detail))


def create_workspace(
    root: Path | str,
    *,
    type: DefinitionType = "evalset",
    git: bool = True,
) -> CreateReport:
    """Create (or complete) a Steward workspace.

    Safe to re-run: existing files are kept, and only what is missing is added. See workflow.md *`steward init` — the deliverable is a directory*.

    Args:
        root: Workspace root. Created if it does not exist.
        type: Definition type, which decides the placeholder's filename.
        git: Initialise a repository when the directory is not already in one.

    Returns:
        What was created, kept, updated, or skipped.

    Raises:
        OSError: If the workspace cannot be written.
    """
    workspace = Workspace.at(root)
    workspace.root.mkdir(parents=True, exist_ok=True)
    report = CreateReport(workspace=workspace)

    _write_template(report, workspace.agents, "agents.md")
    _link_claude(report, workspace)
    _write_template(report, workspace.directives, "_steward.yaml")
    _create_definition(report, workspace, type)
    _update_gitignore(report, workspace)
    _initialize_git(report, workspace, git)
    _open_journal(report, workspace)

    return report


def _relative(workspace: Workspace, path: Path) -> str:
    return path.relative_to(workspace.root).as_posix()


def _template(name: str) -> str:
    return resources.files(f"{__package__}.templates").joinpath(name).read_text("utf-8")


def _write_template(report: CreateReport, path: Path, template: str) -> None:
    name = _relative(report.workspace, path)
    if path.exists():
        report.record(name, Outcome.KEPT)
        return
    path.write_text(_template(template), encoding="utf-8")
    report.record(name, Outcome.CREATED)


def _link_claude(report: CreateReport, workspace: Workspace) -> None:
    """Point `CLAUDE.md` at `AGENTS.md`.

    A symlink keeps them from drifting. Where the platform will not make one — Windows without developer mode — an `@AGENTS.md` import achieves the same thing for the one tool that reads this filename, and is a pointer rather than a second copy.
    """
    name = _relative(workspace, workspace.claude)
    # islink rather than exists: a symlink to a not-yet-written target reads as
    # absent, and re-running init must not then try to create it again
    if workspace.claude.exists() or workspace.claude.is_symlink():
        report.record(name, Outcome.KEPT)
        return
    try:
        workspace.claude.symlink_to(workspace.agents.name)
        report.record(name, Outcome.CREATED)
    except OSError:
        workspace.claude.write_text("@AGENTS.md\n", encoding="utf-8")
        report.record(
            name, Outcome.CREATED, "as an import; this platform declined a symlink"
        )


def _create_definition(
    report: CreateReport, workspace: Workspace, type: DefinitionType
) -> None:
    """Place an empty definition, unless the workspace already has one.

    Empty on purpose. A placeholder says where the definition goes; a scaffolded example would be a guess at what is being measured, and has to be deleted before it can be useful.
    """
    if (existing := workspace.find_definition()) is not None:
        report.record(_relative(workspace, existing), Outcome.KEPT)
        return
    path = workspace.definition(type)
    path.touch()
    report.record(
        _relative(workspace, path), Outcome.CREATED, "empty; write your eval set here"
    )


def ensure_gitignore(workspace: Workspace) -> list[str]:
    """Ensure the workspace's own ignore rules are present, without disturbing any already there.

    Public because `init` is not the only caller that needs it. A workspace created before an entry existed does not have it, and nothing re-runs `init` — so the command that makes a path load-bearing is the one that has to know it is ignored. `steward timer arm` is the case that matters: it tells people to put credentials in `.env`, and giving that advice for a path git would track is worse than not giving it.

    **What this cannot fix is a file already tracked.** An ignore rule does not untrack anything, so a `.env` committed before the rule arrived stays committed. Adding the rule is still the whole of what Steward can do about it from here.

    Args:
        workspace: The workspace.

    Returns:
        The entries this added, in `GITIGNORE_ENTRIES` order. Empty where every one was already there.

    Raises:
        OSError: If `.gitignore` cannot be read or written.
    """
    path = workspace.root / ".gitignore"
    if not path.exists():
        path.write_text(_template("gitignore.txt"), encoding="utf-8")
        return list(GITIGNORE_ENTRIES)

    current = path.read_text(encoding="utf-8")
    present = {line.strip() for line in current.splitlines()}
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return []

    separator = "" if current.endswith("\n") else "\n"
    addition = "\n".join(missing)
    path.write_text(f"{current}{separator}\n# Steward workspace\n{addition}\n", "utf-8")
    return missing


def _update_gitignore(report: CreateReport, workspace: Workspace) -> None:
    existed = (workspace.root / ".gitignore").exists()
    added = ensure_gitignore(workspace)
    if not existed:
        report.record(".gitignore", Outcome.CREATED)
    elif added:
        report.record(".gitignore", Outcome.UPDATED, f"added {', '.join(added)}")
    else:
        report.record(".gitignore", Outcome.KEPT)


def _in_repository(directory: Path) -> bool:
    """Whether a directory is already inside a git repository.

    Walks up looking for `.git`, which needs no git binary — and accepts a *file* as well as a directory, because that is what a worktree or submodule has.
    """
    return any(
        (candidate / ".git").exists() for candidate in (directory, *directory.parents)
    )


def _initialize_git(report: CreateReport, workspace: Workspace, git: bool) -> None:
    """Start a repository, unless there is one already or the user declined.

    A workspace created inside an existing project belongs to that project's repository; nesting one there is a footgun rather than a convenience. A machine without git is an ordinary condition — the S3 sync plays git's role there — so it is announced rather than raised.
    """
    if not git:
        report.record("git", Outcome.SKIPPED, "--no-git")
        return
    if _in_repository(workspace.root):
        report.record("git", Outcome.KEPT, "already in a repository")
        return
    if shutil.which("git") is None:
        report.record("git", Outcome.SKIPPED, "git is not installed")
        return
    result = subprocess.run(
        ["git", "init", "--quiet", str(workspace.root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        report.record(
            "git", Outcome.SKIPPED, (result.stderr or "git init failed").strip()
        )
    else:
        report.record("git", Outcome.CREATED, "repository initialised")


def _open_journal(report: CreateReport, workspace: Workspace) -> None:
    """Write the journal's first event.

    The journal is what makes the directory a workspace: it is durable, Steward-specific, and present from this moment until the directory is gone, which is what lets `Workspace.find` recognise one. Opening it with a real record rather than an empty file means the record of the workspace starts where the workspace does.
    """
    if workspace.journal.exists():
        report.record("journal.jsonl", Outcome.KEPT)
        return
    definition = workspace.find_definition()
    append_event(
        workspace.journal,
        "initialized",
        definition=_relative(workspace, definition) if definition else None,
    )
    report.record("journal.jsonl", Outcome.CREATED)
