"""`analysis.md` — the one file Steward and the agent both write.

Everything worth defending here is about the **merge**, because the merge is the only thing in the codebase that rewrites part of a file whose other part is somebody's work. So the assertions are mostly of the form *this text came back byte-identical*: prose survives ten turns, a section for a task the manifest dropped survives, and a section whose markers do not pair is not touched at all rather than repaired on a guess.

The turn-level cases run the real tend against a real workspace, because the claim they carry — that the file reaches `log_dir` through the ordinary sync, and that a section arrives with a task's first log — is about the wiring rather than about the merge.
"""

from pathlib import Path

import pytest
from inspect_steward._signoff import check
from inspect_steward._tend import Owner, Verdict
from inspect_steward._tend import turn as turn_module
from inspect_steward._tend.analysis_md import (
    BEGIN,
    END,
    PLACEHOLDER,
    Section,
    merge_analysis,
)
from inspect_steward._tend.items import UNWRITTEN
from inspect_steward._workspace import COLLECTED, Workspace, append_event

from .._logs import SynthTask, write_log
from ..schedule.test_tend import prepared, turn

TASK = SynthTask("probe", samples=4)

ONE = Section(identifier="probe_aaa", key="probe@openai/gpt-5", facts=("48 of 50",))
TWO = Section(identifier="other_bbb", key="other@openai/gpt-5", facts=("10 of 10",))

PROSE = "Both flagged samples tried to read the grader and failed."


def written(*sections: Section, prose: str = PROSE) -> str:
    """A file as it looks once somebody has written into every section."""
    body = merge_analysis("", list(sections)).body
    return body.replace(PLACEHOLDER, prose)


def block(identifier: str) -> tuple[str, str]:
    return BEGIN.format(identifier=identifier), END


def test_a_first_turn_creates_the_file_with_a_section_per_task() -> None:
    merged = merge_analysis("", [ONE, TWO])

    assert "# analysis" in merged.body
    for section in (ONE, TWO):
        begin, end = block(section.identifier)
        assert f"## {section.key}" in merged.body
        assert begin in merged.body and end in merged.body
    assert "- 48 of 50" in merged.body
    # and every one of them is owed a write-up, because none has any
    assert merged.unwritten == {
        ONE.identifier: ONE.key,
        TWO.identifier: TWO.key,
    }


def test_a_later_turn_rewrites_only_what_is_between_the_markers() -> None:
    existing = written(ONE)
    moved = Section(identifier=ONE.identifier, key=ONE.key, facts=("50 of 50",))

    merged = merge_analysis(existing, [moved])

    assert "- 50 of 50" in merged.body
    assert "- 48 of 50" not in merged.body
    assert PROSE in merged.body
    assert merged.unwritten == {}


def test_prose_is_byte_identical_across_ten_turns() -> None:
    # the whole contract in one assertion: a section written into on turn one
    # comes back unchanged on turn ten, however much the facts moved
    body = written(ONE, TWO)
    before = body

    for turn_number in range(10):
        facts = (f"{turn_number} of 50",)
        body = merge_analysis(
            body,
            [
                Section(ONE.identifier, ONE.key, facts),
                Section(TWO.identifier, TWO.key, facts),
            ],
        ).body

    assert body.count(PROSE) == before.count(PROSE) == 2
    assert _outside_markers(body) == _outside_markers(before)


def _outside_markers(body: str) -> list[str]:
    """Every line of the document that is not inside a facts block."""
    kept: list[str] = []
    inside = False
    for line in body.splitlines():
        if line.startswith("<!-- steward:begin"):
            inside = True
        elif line.startswith("<!-- steward:end"):
            inside = False
        elif not inside:
            kept.append(line)
    return kept


def test_an_unpaired_marker_leaves_that_section_untouched() -> None:
    # never guess at a boundary in a file whose other half is somebody's work
    begin, _ = block(ONE.identifier)
    existing = f"# analysis\n\n## {ONE.key}\n\n{begin}\n- stale\n\n{PROSE}\n"

    merged = merge_analysis(existing, [ONE])

    assert "- stale" in merged.body
    assert "- 48 of 50" not in merged.body
    assert merged.damaged == (ONE.identifier,)
    # and it is not reported as unwritten either: nothing here read that text,
    # so *you have not written this* is a claim it cannot make
    assert merged.unwritten == {}


def test_a_task_the_manifest_no_longer_names_keeps_its_section() -> None:
    # the file is durable; a removed task's investigation is still what happened
    existing = written(ONE, TWO)

    merged = merge_analysis(existing, [ONE])

    assert f"## {TWO.key}" in merged.body
    assert "- 10 of 10" in merged.body
    assert merged.body.count(PROSE) == 2


def test_a_new_task_appends_without_disturbing_the_others() -> None:
    existing = written(ONE)

    merged = merge_analysis(existing, [ONE, TWO])

    assert merged.body.index(f"## {ONE.key}") < merged.body.index(f"## {TWO.key}")
    assert PROSE in merged.body
    assert merged.unwritten == {TWO.identifier: TWO.key}


def test_a_comment_is_not_a_write_up() -> None:
    # which is what lets the placeholder be a prompt rather than an answer: a
    # placeholder that satisfied the check would let the entry go unwritten
    fresh = merge_analysis("", [ONE])

    assert PLACEHOLDER in fresh.body
    assert fresh.unwritten == {ONE.identifier: ONE.key}


def test_the_merge_is_idempotent() -> None:
    once = merge_analysis(written(ONE), [ONE]).body

    assert merge_analysis(once, [ONE]).body == once


class TestBytesOutsideTheBlockSurvive:
    """*Byte-identical* has to mean bytes, or the promise is worth nothing.

    Splitting a document into bare lines and re-joining them on a newline of one's own choosing rewrites every line ending of a CRLF file and eats whatever trailed the last one. That is not a merge touching only what is between the markers — it is whole-file churn in a document under version control, and it arrives as a diff nobody made.
    """

    def crlf(self) -> str:
        return written(ONE).replace("\n", "\r\n")

    def test_a_crlf_file_stays_crlf(self) -> None:
        existing = self.crlf()
        moved = Section(ONE.identifier, ONE.key, ("50 of 50",))

        body = merge_analysis(existing, [moved]).body

        assert "\r\n" in body
        assert "\n" not in body.replace("\r\n", "")
        assert f"{PROSE}\r\n" in body

    def test_a_crlf_file_gains_crlf_sections(self) -> None:
        body = merge_analysis(self.crlf(), [ONE, TWO]).body

        assert f"## {TWO.key}\r\n" in body
        assert "\n" not in body.replace("\r\n", "")

    def test_trailing_bytes_outside_the_block_are_not_trimmed(self) -> None:
        # a file ending in blank lines is a file somebody left that way, and
        # trimming them is a diff nobody made
        existing = written(ONE) + "\n\n"
        moved = Section(ONE.identifier, ONE.key, ("50 of 50",))

        body = merge_analysis(existing, [moved]).body

        tail = existing[existing.index(PROSE) :]
        assert body.endswith(tail)

    def test_a_file_with_no_final_newline_gains_exactly_one(self) -> None:
        # enough that an appended heading starts its own line, and no more
        existing = written(ONE).rstrip("\n")

        body = merge_analysis(existing, [ONE, TWO]).body

        assert f"{PROSE}## {TWO.key}" not in body
        assert body.startswith(f"{existing}\n## {TWO.key}\n")

    def test_everything_outside_the_blocks_is_byte_identical(self) -> None:
        # the strongest form of the claim, over ten turns of moving facts
        before = self.crlf()
        body = before
        for turn_number in range(10):
            body = merge_analysis(
                body, [Section(ONE.identifier, ONE.key, (f"{turn_number} of 50",))]
            ).body

        assert _split_on_blocks(body) == _split_on_blocks(before)


def _split_on_blocks(body: str) -> list[str]:
    """The document with every facts block excised, terminators and all."""
    kept: list[str] = []
    inside = False
    for line in body.splitlines(keepends=True):
        if line.startswith("<!-- steward:begin"):
            inside = True
        elif line.startswith("<!-- steward:end"):
            inside = False
        elif not inside:
            kept.append(line)
    return kept


class TestThroughATurn:
    """The wiring: when a section arrives, what raises an item, and where the file goes."""

    def workspace(self, tmp_path: Path, *, collected: bool = True) -> Workspace:
        workspace, _ = prepared(tmp_path, [TASK])
        if collected:
            # the obligation starts when an agent attaches, and not before
            append_event(workspace.journal, COLLECTED, position=0)
        return workspace

    def test_a_section_arrives_with_the_tasks_first_log(self, tmp_path: Path) -> None:
        workspace = self.workspace(tmp_path)

        assert not workspace.analysis.exists()

        write_log(workspace.logs, TASK)
        turn(workspace)

        body = workspace.analysis.read_text(encoding="utf-8")
        assert f"## {TASK.name}" in body
        assert "steward:begin" in body

    def test_an_empty_section_is_the_agents_item_and_not_a_gate(
        self, tmp_path: Path
    ) -> None:
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)

        result = turn(workspace)

        items = [item for item in result.items if item.kind == UNWRITTEN]
        assert len(items) == 1
        assert items[0].owner is Owner.AGENT
        # writing *looked, nothing here* is the deliverable, so there is no way
        # to wave it past
        assert not items[0].acknowledgeable
        # and it keeps the run at ⚠️ rather than reading as finished
        assert result.verdict is Verdict.ATTENTION
        # but it does not stand between an operator and their attestation: holding
        # a signature hostage to an agent's prose is the wrong trade
        assert check(result, None) == []

    def test_writing_the_section_closes_the_item(self, tmp_path: Path) -> None:
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)
        turn(workspace)
        body = workspace.analysis.read_text(encoding="utf-8")
        workspace.analysis.write_text(
            body.replace(PLACEHOLDER, "Nothing unusual; every sample scored."),
            encoding="utf-8",
        )

        result = turn(workspace)

        assert [item for item in result.items if item.kind == UNWRITTEN] == []
        assert "Nothing unusual; every sample scored." in workspace.analysis.read_text(
            encoding="utf-8"
        )

    def test_prose_saved_while_the_turn_is_acting_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other author is not serialized by the claim, and the turn is long.

        The facts are composed near the top of the turn, because the items have
        to report what is unwritten; the write happens at the end, after spawns,
        requeues, invalidations and archive moves — minutes on a busy turn. A
        operator or an agent with the file open saves somewhere in the middle, and
        an atomic replace of a snapshot taken before all of that is their work
        gone. So the write re-reads.

        `_act` is where the interval opens, which is why the save is injected
        there rather than at an arbitrary point.
        """
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)
        turn(workspace)

        saved = "Ran this down: the two flagged samples never reached the grader."
        acting = turn_module._act

        def act(*args: object, **kwargs: object) -> object:
            body = workspace.analysis.read_text(encoding="utf-8")
            workspace.analysis.write_text(
                body.replace(PLACEHOLDER, saved), encoding="utf-8"
            )
            return acting(*args, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]

        monkeypatch.setattr(turn_module, "_act", act)
        turn(workspace)

        assert saved in workspace.analysis.read_text(encoding="utf-8")

    def test_a_hand_driven_run_is_owed_no_write_up(self, tmp_path: Path) -> None:
        # `Supervision.ever_armed`'s reasoning applied to the other expectation:
        # a workspace no agent ever attached to has nobody the item addresses
        workspace = self.workspace(tmp_path, collected=False)
        write_log(workspace.logs, TASK)

        result = turn(workspace)

        assert [item for item in result.items if item.kind == UNWRITTEN] == []
        # the file is still kept current — the facts are Steward's either way
        assert workspace.analysis.exists()

    def test_it_reaches_the_log_directory_through_the_ordinary_sync(
        self, tmp_path: Path
    ) -> None:
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)
        target = tmp_path / "remote"

        turn(workspace, sync=str(target))

        assert (target / "analysis.md").exists()

    def test_a_crlf_file_keeps_its_line_endings_through_a_tend(
        self, tmp_path: Path
    ) -> None:
        # the merge preserves the authored bytes only if it is handed them.
        # Universal-newline mode strips every CR before the merge sees one, and
        # the atomic replace then rewrites every line in the file to settle one
        # bullet — whole-file churn in somebody's editor, from a turn that
        # changed a number
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)
        turn(workspace)
        authored = workspace.analysis.read_text(encoding="utf-8").replace(
            PLACEHOLDER, "Ran it down; nothing in the transcripts."
        )
        workspace.analysis.write_bytes(authored.replace("\n", "\r\n").encode("utf-8"))

        turn(workspace)

        body = workspace.analysis.read_bytes()
        assert b"Ran it down; nothing in the transcripts.\r\n" in body
        # and not one bare newline survives anywhere, generated block included
        assert b"\n" not in body.replace(b"\r\n", b"")

    def test_a_file_that_will_not_decode_does_not_take_the_turn_down(
        self, tmp_path: Path
    ) -> None:
        # `UnicodeDecodeError` is a `ValueError` and escapes an `OSError`
        # handler entirely, so a file saved in another encoding would fail every
        # `status` and every `tend` on the workspace — over a document nothing
        # else in the turn depends on. It is the same answer a permissions error
        # gets: *this exists and I cannot read it*, so leave it alone
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)
        turn(workspace)
        authored = b"## notes\n\nthe mod\xe8le refused, and \xff is no encoding\n"
        workspace.analysis.write_bytes(authored)

        result = turn(workspace)

        assert workspace.analysis.read_bytes() == authored
        assert "analysis.md" not in result.rendered

    def test_a_file_that_will_not_read_is_left_completely_alone(
        self, tmp_path: Path
    ) -> None:
        # the merge composes from empty and the write is an atomic replace, so
        # reading a permissions error as *no file yet* would overwrite an
        # investigation with a stub
        workspace = self.workspace(tmp_path)
        write_log(workspace.logs, TASK)
        turn(workspace)
        workspace.analysis.unlink()
        workspace.analysis.mkdir()  # every read of it now raises

        result = turn(workspace)

        assert workspace.analysis.is_dir()
        assert "analysis.md" not in result.rendered
