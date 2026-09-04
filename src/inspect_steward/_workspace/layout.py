from dataclasses import dataclass
from pathlib import Path

from .._evalset.detect import DefinitionType, detect_definition_type
from .._evalset.manifest import Manifest

JOURNAL = "journal.jsonl"
"""The durable record, and the file that marks a directory as a workspace."""

DEFINITION_NAMES: dict[DefinitionType, str] = {
    "evalset": "evalset.py",
    "flow": "config.py",
    "hawk": "hawk.yaml",
}
"""Conventional definition filename per type, and the name `init` scaffolds.

Flow's own name, because a flow spec is a file the author names themselves and `config.py` is what flow's documentation calls it in every example. Discovery does not stop here — see `find_definition`, which reads any other Python file in the root."""

DEFINITION_ALIASES: tuple[str, ...] = ("flow.yaml", "flow.yml", "flow.py")
"""Other conventional names discovery answers to, after `DEFINITION_NAMES`.

`flow.yaml` is what Steward scaffolded before flow specs were written in Python, so a workspace created then keeps working without being renamed."""

AUTO_INCLUDE_NAME = "_flow.py"
"""Flow's own auto-include file, which is never the definition.

Flow discovers `_flow.py` in the spec's directory and every parent and merges it into whatever spec is being run — it holds shared defaults, `@step` functions and log filters. It imports `inspect_flow`, so content detection classifies it as a flow definition, and a workspace holding one alongside a spec would otherwise be ambiguous."""

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
    """A Steward workspace: the directory an operator and an agent co-inhabit.

    Every path in the layout is derived here so that no other module builds one from a string. See workflow.md *`steward init` — the deliverable is a directory* for what each file is for, and *Three categories, and the one that matters* for which of them can be recovered if lost.
    """

    root: Path
    """Workspace root (absolute)."""

    # authored — the operator's own work, and never overwritten
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

        Settings Steward acts on unattended, and a `policies` key an agent interprets when it arrives. Steward proposes changes and writes only what the operator approved (workflow.md, *The one file Steward may write but never decide*) — a rule under more pressure now the whole file is machine-shaped, and no less binding for it. The underscore sorts it to the top of a listing, beside `AGENTS.md`.
        """
        return self.root / "_steward.yaml"

    # durable machine state — nothing here can be rebuilt
    @property
    def journal(self) -> Path:
        """`journal.jsonl` — the append-only record of what was observed and decided."""
        return self.root / JOURNAL

    @property
    def analysis(self) -> Path:
        """`analysis.md` — what the numbers mean, written by the agent and kept current by Steward.

        **Durable, and the only file here that is neither party's alone.** `status.md` and `anomalies.md` are Steward's and are rewritten whole every turn; `AGENTS.md` and `_steward.yaml` are the operator's and are never touched. This one is co-authored (workflow.md §12.7): Steward keeps a block of facts current inside each task's section, and every word outside those markers is somebody's investigation — quoted, argued, and not regenerable from anything. Losing it loses work.

        At the root beside `status.md`, so the sync carries it to a remote reader with no rule of its own.
        """
        return self.root / "analysis.md"

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
    def classed(self) -> Path:
        """`.steward/classed.json` — what previous turns classified, per log.

        Disposable exactly like `observed.json`: instance identity is content-derived and the journal's dedupe ledger is what prevents double-counting, so losing this costs one slow turn of re-reads and never a wrong answer.
        """
        return self.state / "classed.json"

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
    def smoke(self) -> Path:
        """`.steward/smoke/` — where a rehearsal's logs land, and never `logs/`.

        **Its own directory because two-sample logs are indistinguishable from results.** A truncated log written into `logs/` reads as a real one to `samples_df`, to the viewer, and to whoever analyses the eval six months from now (workflow.md §7.1). Under `.steward/` rather than beside `log_dir` because `log_dir` is frequently S3, and a rehearsal has no business writing throwaway objects to a bucket — slower, billable, and leaving junk that needs a lifecycle rule to clear.

        The cleanup question then answers itself: each smoke clears the previous one, a failed smoke's logs stay for as long as anyone wants to read them, and everything goes when `.steward/` goes. `inspect view --log-dir .steward/smoke` works on it like any other directory.

        **Nothing else in Steward can see it, which is the point.** `observe_logs` lists non-recursively and is only ever pointed at `manifest.log_dir` or its archive sibling; the sync carries top-level files and skips dotted names. So a rehearsal cannot contribute a log to the run, and cannot be mirrored into a bucket, by construction rather than by discipline.

        Its machine state goes inside it too (`smoke_workers`, `smoke_inflight`), so that one `rmtree` is the whole of clearing a rehearsal.
        """
        return self.state / "smoke"

    @property
    def smoke_workers(self) -> Path:
        """`.steward/smoke/workers/` — a rehearsal's selection documents and worker output.

        **Separate from `workers/`, and this is a correctness boundary rather than tidiness.** `resolve_inflight` bounds its process-table scan by the workers directory it is given, so a rehearsal's workers sharing the run's directory would be workers the run's own tend believes are its.
        """
        return self.smoke / "workers"

    @property
    def smoke_inflight(self) -> Path:
        """`.steward/smoke/inflight.jsonl` — what the rehearsal spawned.

        **Separate from `inflight.jsonl` for a reason measured rather than anticipated.** The record accounts for *spent attempts per identifier*, and `reconcile` stops respawning a task after two of them. A rehearsal writing into the run's record therefore spends the run's attempt budget: two smokes of a two-task definition left both tasks stalled before the real launch had run a single sample. A rehearsal's workers are not attempts at the run, and the record that says so is this one.
        """
        return self.smoke / "inflight.jsonl"

    @property
    def marks(self) -> Path:
        """`.steward/marks/` — where a marking ruling is carried out, and never `logs/`.

        An `exclude` or `zero` ruling is written into a landed log by a detached runner (`_marks.run`), and a zero needs a scratch side run to obtain the scorer's own verdict on an empty attempt. Everything that side run produces is disposable and lands here, under the same argument `smoke` makes: a two-sample scratch log written into `logs/` reads as a result to every tool that opens the directory. One run directory per attempt (`marks_run`), so a failed run's output stays readable until `.steward/` goes.
        """
        return self.state / "marks"

    @property
    def marks_workers(self) -> Path:
        """`.steward/marks/workers/` — a side run's selection documents and worker output.

        Separate from `workers/` for the reason `smoke_workers` is: `resolve_inflight` bounds its process-table scan by the workers directory it is given, and a side worker under the run's directory would be a worker the run's own tend believes is its.
        """
        return self.marks / "workers"

    @property
    def marks_inflight(self) -> Path:
        """`.steward/marks/inflight.jsonl` — what the side runs spawned, apart from the run's own attempt budget (`smoke_inflight`)."""
        return self.marks / "inflight.jsonl"

    @property
    def marks_runs(self) -> Path:
        """`.steward/marks/runs.jsonl` — every marking run, appended before and after each spawn (`_marks.state`)."""
        return self.marks / "runs.jsonl"

    def marks_run(self, run: str) -> Path:
        """`.steward/marks/<run>/` — one marking run's scratch: its output log, its capture guard, and a zero's side logs."""
        return self.marks / run

    @property
    def inflight(self) -> Path:
        """`.steward/inflight.jsonl` — what was spawned, appended before each launch.

        The journal's opposite in every way that matters: machine-only, and rebuildable from the process table on the next resolve, which is why it lives under `.steward/` (execution.md, *Detachment and the in-flight record*).
        """
        return self.state / "inflight.jsonl"

    @property
    def env(self) -> Path:
        """`.env` — credentials a scheduled tend and its workers both read.

        Not written by Steward and not required to exist. Named here because inspect loads it for free: `find_dotenv(usecwd=True)` searches up from a worker's cwd, which is this directory.

        **The nearest candidate rather than the only one.** That search walks *up*, so a `.env` in a parent directory is loaded where this one is absent, and arming checks whichever the walk lands on (`_timer.env.resolved`) rather than this path alone. What this path remains is where a credential the walk did not find should go — nearest wins, so writing one here also shadows anything above it.
        """
        return self.root / ".env"

    @property
    def status(self) -> Path:
        """`status.md` — rewritten by every tend."""
        return self.root / "status.md"

    @property
    def anomalies(self) -> Path:
        """`anomalies.md` — the caveats that reached the final data, rewritten by every tend.

        At the root beside `status.md` rather than under `.steward/`, and that does two things at once: an operator reading the directory finds it where a write-up's footnotes belong, and the sync carries every non-dotfile at the top level, so it reaches a remote reader with no propagation rule of its own.
        """
        return self.root / "anomalies.md"

    @property
    def log(self) -> Path:
        """`.steward/steward.log` — whether the machinery worked, as opposed to what it found.

        Disposable, and here rather than at the root because that is the category it belongs to. It used to sit at the top level for one stated reason — so the sync would carry it out without an exception to its own deny list — which is Steward dodging a rule Steward wrote. The sync names it instead, and the root is left for what an operator authored and what an operator reads.
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
        """Locate this workspace's definition.

        By name first, because a name is unambiguous and costs no reads: a conventional filename in `DEFINITION_NAMES`, then `DEFINITION_ALIASES`. A placeholder `init` has scaffolded but nobody has written yet is found here, which is why the name pass comes first — an empty file validates as both a flow spec and a hawk config and would be reported ambiguous by content.

        Failing that, any other Python file in the root is read: a flow spec is a file its author names, so `swebench.py` is as ordinary a name as `config.py` and a workspace holding one has a definition. `AUTO_INCLUDE_NAME` is skipped, and a file that classifies as neither kind is passed over rather than raising, since a workspace may hold Python that is not a definition at all.

        Returns:
            The definition, or `None` when the workspace has none, and also when several Python files each look like one — a caller with no way to choose between them should say so rather than pick.
        """
        named = next(
            (
                path
                for name in (*DEFINITION_NAMES.values(), *DEFINITION_ALIASES)
                if (path := self.root / name).exists()
            ),
            None,
        )
        if named is not None:
            return named
        found = self.definition_candidates()
        return found[0] if len(found) == 1 else None

    def definition_candidates(self) -> list[Path]:
        """Python files in the root that read as an eval set definition.

        Sorted by name, so two callers looking at one directory report the same list. Excludes `AUTO_INCLUDE_NAME` and anything that does not classify.

        Returns:
            The candidates, which may be empty.
        """
        candidates: list[Path] = []
        for path in sorted(self.root.glob("*.py")):
            if path.name == AUTO_INCLUDE_NAME:
                continue
            try:
                detect_definition_type(path)
            except (ValueError, OSError):
                continue
            candidates.append(path)
        return candidates

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


def resolve_log_dir(
    workspace: Workspace, manifest: Manifest, log_root: str | None = None
) -> str:
    """Where this run's logs go: the definition's own directory, a machine's root, or the workspace's.

    Three rungs, in order, and **the root supplies a default rather than modifying an answer**. A definition that names a `log_dir` is the single source of truth for where its results go, so the root does not rebase it, prefix it, or otherwise touch it — silence is the only thing the root answers. That is what keeps this consistent with the rule one level out: Steward refuses every *override* of a stated `log_dir` (`INSPECT_LOG_DIR` among them, at launch) and supplies a *default* where the definition states none.

    Called once, by `launch`, whose answer is committed to the manifest and read back by every later tend. Deriving it per turn would mean a scheduled tend — which inherits almost no environment — resolving a different directory from the one its fleet is writing to (`Manifest.log_dir`).

    Args:
        workspace: The workspace, whose directory name is the run's name under a root.
        manifest: The captured manifest, whose `options` carry the definition's own `log_dir` when it named one.
        log_root: A machine's root for eval logs, as `resolve_log_root` settled it, or `None` for none.

    Returns:
        The log directory, as a local path or a URL. Its archive is `archive_dir()` of it.
    """
    configured = manifest.options.get("log_dir")
    if isinstance(configured, str) and configured:
        if _remote(configured) or Path(configured).is_absolute():
            return configured
        # a relative log_dir is relative to where the definition was captured,
        # which for a workspace's own definition is the workspace
        return str(workspace.root / configured)
    if log_root:
        # the workspace's directory name, which is what keeps two workspaces
        # sharing one root from sharing one directory -- and they must not,
        # since each propagates its own `status.md` and `journal.jsonl` into it
        # (`_workspace.sync`). Two workspaces *named* the same still collide,
        # which is a fact about what they were named
        if _remote(log_root):
            return f"{log_root.rstrip('/')}/{workspace.root.name}"
        # resolved rather than left relative: a root is a location on this
        # machine, and one read from the environment of whatever shell typed
        # `launch` would otherwise mean a different directory per shell. Once
        # here it is recorded, so this is the only time it can vary at all
        return str(Path(log_root).expanduser().resolve() / workspace.root.name)
    return str(workspace.logs)


def _remote(location: str) -> bool:
    """Whether a location names a filesystem other than this machine's."""
    return "://" in location
