"""The three anomaly verbs, and every refusal they owe.

A ruling is the most consequential append in the workflow, so most of what is tested here is what the verbs refuse: an unknown class, a settled one, `accept` on an errored population, an effect on a disposition that marks nothing, a proposal that would re-run into broken machinery. The happy paths assert what lands in the journal, since the journal is the only record a decision leaves.

Entered as a shell would enter them, over a real workspace with hand-journalled windows.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from inspect_steward._cli.main import steward
from inspect_steward._evalset.classify import scan_class
from inspect_steward._workspace import (
    INSTANCE,
    INVESTIGATING,
    OPENED,
    PROPOSAL,
    RULING,
    Workspace,
    append_event,
    create_workspace,
    read_journal,
)

from .._logs import SynthSample, SynthTask, write_log
from ..schedule.test_tend import prepared, turn

CLASS_A = "error:TimeoutError@openai/_client.py:post"
CLASS_B = "error:ValueError@evals/scorer.py:score"
DONE = SynthTask("done")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A finished run with two open anomaly classes on the record."""
    create_workspace(tmp_path, git=False)
    done = DONE
    workspace, _ = prepared(tmp_path, [done])
    write_log(workspace.logs, done)
    opened(workspace, CLASS_A, count=3)
    opened(workspace, CLASS_B, count=2)
    monkeypatch.chdir(workspace.root)
    return workspace


def opened(
    workspace: Workspace,
    class_key: str,
    *,
    count: int,
    substrate: bool = False,
    kind: str = "error",
    refs: list[str] | None = None,
    tasks: list[str] | None = None,
) -> None:
    window: dict[str, Any] = {
        "class": class_key,
        "kind": kind,
        "substrate": substrate,
    }
    append_event(workspace.journal, OPENED, **window)
    instances: dict[str, Any] = {
        "class": class_key,
        "count": count,
        "refs": refs
        if refs is not None
        else [f"ev1:s{n}:1:u{n}" for n in range(count)],
        "exemplar": "TimeoutError('too slow')",
    }
    if tasks is not None:
        instances["tasks"] = tasks
    append_event(workspace.journal, INSTANCE, **instances)


def proposal_id(output: str) -> str:
    return next(word for word in output.split() if word.startswith("prop-")).rstrip(":")


def run(*argv: str) -> tuple[int, str]:
    result = CliRunner().invoke(steward, list(argv))
    return result.exit_code, result.output


def rulings(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in read_journal(workspace.journal).events
        if event.type == RULING
    ]


class TestRule:
    def test_a_ruling_lands_one_event_per_class_with_the_account(
        self, workspace: Workspace
    ) -> None:
        code, output = run(
            "rule",
            "error:TimeoutError",
            "error:ValueError",
            "--disposition",
            "rerun",
            "--reason",
            "provider outage overnight",
            "--by",
            "kaia",
        )

        assert code == 0
        landed = rulings(workspace)
        assert [entry["class"] for entry in landed] == [CLASS_A, CLASS_B]
        assert all(entry["disposition"] == "rerun" for entry in landed)
        assert all(entry["reason"] == "provider outage overnight" for entry in landed)
        assert all(entry["by"] == "kaia" for entry in landed)
        # the finding in words first, the key last as the address
        assert "ruled TimeoutError errors: rerun" in output
        assert f"`{CLASS_A}`" in output

    def test_by_defaults_to_the_person_the_repository_names(
        self, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # an agent relaying the operator whose shell this is should not have to
        # ask their name -- the repository already carries it
        git = shutil.which("git")
        if git is None:
            pytest.skip("git is not installed")
        monkeypatch.setenv(
            "GIT_CONFIG_GLOBAL", str(workspace.root / "no-global-gitconfig")
        )
        subprocess.run([git, "init", "-q", str(workspace.root)], check=True)
        subprocess.run(
            [git, "-C", str(workspace.root), "config", "user.name", "Kaia Example"],
            check=True,
        )

        code, output = run(
            "rule", CLASS_A, "--disposition", "rerun", "--reason", "provider outage"
        )

        assert code == 0, output
        assert rulings(workspace)[0]["by"] == "Kaia Example"
        # and the terminal says so too. It used to echo the raw `--by`, which
        # is `None` on exactly this path, so the one confirmation an operator
        # reads disagreed with the record it was confirming
        assert "(by Kaia Example)" in output

    def test_an_unknown_class_is_refused_listing_what_is_open(
        self, workspace: Workspace
    ) -> None:
        code, output = run(
            "rule",
            "error:Nothing",
            "--disposition",
            "rerun",
            "--reason",
            "r",
            "--by",
            "b",
        )

        assert code != 0
        assert "no open class matches" in output
        assert CLASS_A in output and CLASS_B in output

    def test_an_ambiguous_prefix_is_refused_listing_the_matches(
        self, workspace: Workspace
    ) -> None:
        code, output = run(
            "rule", "error:", "--disposition", "rerun", "--reason", "r", "--by", "b"
        )

        assert code != 0
        assert "matches 2 classes" in output

    def test_a_settled_class_names_its_ruling(self, workspace: Workspace) -> None:
        run(
            "rule",
            CLASS_A,
            "--disposition",
            "dismiss",
            "--reason",
            "one-off",
            "--by",
            "kaia",
        )

        code, output = run(
            "rule", CLASS_A, "--disposition", "rerun", "--reason", "r", "--by", "b"
        )

        assert code != 0
        assert "already settled" in output
        assert "dismiss" in output and "kaia" in output

    def test_accept_on_an_errored_class_is_refused_naming_the_four(
        self, workspace: Workspace
    ) -> None:
        code, output = run(
            "rule",
            CLASS_A,
            "--disposition",
            "accept",
            "--reason",
            "r",
            "--by",
            "b",
            "--effect",
            "stands",
        )

        assert code != 0
        assert "rerun, exclude, zero, or score" in output
        assert rulings(workspace) == []

    def test_a_sample_mark_on_a_task_class_is_refused(
        self, workspace: Workspace
    ) -> None:
        # "1 sample excluded from scoring" for a failed task attempt would be
        # a false sentence in the report; the matrix refuses to record it
        opened(workspace, "task:vanished", count=1, kind="task")

        code, output = run(
            "rule",
            "task:vanished",
            "--disposition",
            "exclude",
            "--reason",
            "r",
            "--by",
            "b",
        )

        assert code != 0
        assert "there is nothing to mark" in output
        assert "task:vanished (task class)" in output
        assert rulings(workspace) == []

    def test_a_sample_mark_on_a_scan_class_is_recorded(
        self, workspace: Workspace
    ) -> None:
        # a flagged sample is one row in the results, so "1 sample excluded
        # from scoring" is the true sentence — and refusing it would leave
        # `accept` and `dismiss` as the only answers to a score known wrong
        scan = scan_class(
            "scoring_integrity", "reward_hacking", task="done", identifier="done@m"
        )
        opened(workspace, scan, count=1, kind="scan")

        code, _ = run(
            "rule",
            scan,
            "--disposition",
            "exclude",
            "--reason",
            "the grader was gamed",
            "--by",
            "kaia",
        )

        assert code == 0
        assert [entry["disposition"] for entry in rulings(workspace)] == ["exclude"]

    def test_accept_on_a_scan_class_is_allowed(self, workspace: Workspace) -> None:
        # unlike an `error:` class: the data is there and readable, and the
        # caveat is what the reader needs rather than a silent exclusion
        scan = scan_class(
            "scoring_integrity", "refusal", task="done", identifier="done@m"
        )
        opened(workspace, scan, count=1, kind="scan")

        code, _ = run(
            "rule",
            scan,
            "--disposition",
            "accept",
            "--reason",
            "the refusal is the result",
            "--by",
            "kaia",
            "--effect",
            "1 sample scored as recorded",
        )

        assert code == 0

    SCANERROR = "scanerror:scoring_integrity:TimeoutError@openai/_client.py:post"

    @pytest.mark.parametrize(
        ("disposition", "refused"),
        [
            # the marks name a sample's data, and what a failed scan left
            # behind is a verdict that is *absent* rather than a row that is
            # wrong -- there is nothing to take out of the scores
            ("exclude", "there is nothing to mark"),
            ("zero", "there is nothing to mark"),
            ("score", "there is nothing to mark"),
            # and there is nothing to re-run: the eval is fine, only the
            # reading of it failed, and Steward has no verb that re-scans
            ("rerun", "the samples behind"),
        ],
    )
    def test_the_only_answers_to_an_unscanned_transcript_are_accept_and_dismiss(
        self, disposition: str, refused: str, workspace: Workspace
    ) -> None:
        opened(workspace, self.SCANERROR, count=2, kind="scanerror")

        code, output = run(
            "rule",
            self.SCANERROR,
            "--disposition",
            disposition,
            "--reason",
            "r",
            "--by",
            "b",
        )

        assert code != 0
        assert refused in output
        assert rulings(workspace) == []

    @pytest.mark.parametrize("disposition", ["accept", "dismiss"])
    def test_accept_and_dismiss_settle_an_unscanned_transcript(
        self, disposition: str, workspace: Workspace
    ) -> None:
        # which is exactly what the retired acknowledgment meant: *these
        # samples were never scanned and the results stand anyway*, now said
        # as a ruling with a disposition on it
        opened(workspace, self.SCANERROR, count=2, kind="scanerror")

        code, _ = run(
            "rule",
            self.SCANERROR,
            "--disposition",
            disposition,
            "--reason",
            "one grader timeout in five hundred; the rest scanned",
            "--by",
            "kaia",
            *(
                ["--effect", "2 transcripts carry no verdict"]
                if disposition == "accept"
                else []
            ),
        )

        assert code == 0
        assert [entry["disposition"] for entry in rulings(workspace)] == [disposition]

    def test_duplicate_arguments_land_one_ruling(self, workspace: Workspace) -> None:
        # two prefixes naming the same class are one decision, not a ruling
        # immediately superseded by its own copy
        code, _ = run(
            "rule",
            "error:Timeout",
            "error:TimeoutError@openai",
            "--disposition",
            "dismiss",
            "--reason",
            "one-off",
            "--by",
            "kaia",
        )

        assert code == 0
        assert [entry["class"] for entry in rulings(workspace)] == [CLASS_A]

    def test_effect_rules_follow_the_disposition(self, workspace: Workspace) -> None:
        # rerun marks nothing, so an effect has nothing to attach to
        code, output = run(
            "rule",
            CLASS_A,
            "--disposition",
            "rerun",
            "--reason",
            "r",
            "--by",
            "b",
            "--effect",
            "x",
        )
        assert code != 0 and "marks nothing" in output

        # exclude composes its own from the window's weight
        code, output = run(
            "rule", CLASS_A, "--disposition", "exclude", "--reason", "r", "--by", "b"
        )
        assert code == 0
        assert rulings(workspace)[-1]["effect"] == "3 samples excluded from scoring"

    def test_json_reports_what_was_ruled(self, workspace: Workspace) -> None:
        code, output = run(
            "rule",
            CLASS_A,
            "--disposition",
            "zero",
            "--reason",
            "grader broke",
            "--by",
            "kaia",
            "--json",
        )

        assert code == 0
        document = json.loads(output)
        assert document["ruled"][0]["class"] == CLASS_A
        assert document["ruled"][0]["effect"] == "3 samples scored zero"

    def test_superseding_a_standing_ruling_is_printed_loudly(
        self, workspace: Workspace
    ) -> None:
        run(
            "rule",
            CLASS_A,
            "--disposition",
            "rerun",
            "--reason",
            "first call",
            "--by",
            "kaia",
        )

        code, output = run(
            "rule",
            CLASS_A,
            "--disposition",
            "dismiss",
            "--reason",
            "second thoughts",
            "--by",
            "rowan",
        )

        assert code == 0
        assert "supersedes the standing rerun ruling by kaia" in output


class TestProposals:
    def propose(self, *classes: str, action: str = "rerun") -> tuple[int, str]:
        return run(
            "propose",
            *classes,
            "--action",
            action,
            "--reason",
            "both are the provider dying",
        )

    def test_a_proposal_snapshots_and_a_bare_answer_covers_it_all(
        self, workspace: Workspace
    ) -> None:
        code, output = self.propose("error:TimeoutError", "error:ValueError")
        assert code == 0
        identifier = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")
        recorded = next(
            event
            for event in read_journal(workspace.journal).events
            if event.type == PROPOSAL
        )
        assert recorded.payload["classes"][CLASS_A]["count"] == 3

        code, output = run(
            "rule", "--proposal", identifier, "--reason", "agreed", "--by", "kaia"
        )

        assert code == 0
        landed = rulings(workspace)
        assert {entry["class"] for entry in landed} == {CLASS_A, CLASS_B}
        assert all(entry["proposal"] == identifier for entry in landed)
        assert all(entry["disposition"] == "rerun" for entry in landed)

    def test_a_partial_answer_leaves_the_remainder_proposed(
        self, workspace: Workspace
    ) -> None:
        _, output = self.propose("error:TimeoutError", "error:ValueError")
        identifier = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")

        code, _ = run(
            "rule",
            "--proposal",
            identifier,
            "error:TimeoutError",
            "--reason",
            "this half is clear",
            "--by",
            "kaia",
        )

        assert code == 0
        assert [entry["class"] for entry in rulings(workspace)] == [CLASS_A]
        # the remainder still answers by the same proposal
        code, _ = run(
            "rule", "--proposal", identifier, "--reason", "and the rest", "--by", "kaia"
        )
        assert code == 0
        assert [entry["class"] for entry in rulings(workspace)] == [CLASS_A, CLASS_B]

    def test_a_class_outside_the_proposal_is_refused(
        self, workspace: Workspace
    ) -> None:
        _, output = self.propose("error:TimeoutError")
        identifier = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")

        code, output = run(
            "rule",
            "--proposal",
            identifier,
            "error:ValueError",
            "--reason",
            "r",
            "--by",
            "b",
        )

        assert code != 0
        assert "not covered" in output

    def test_an_unknown_proposal_is_refused(self, workspace: Workspace) -> None:
        code, output = run(
            "rule", "--proposal", "prop-missing", "--reason", "r", "--by", "b"
        )

        assert code != 0
        assert "no live proposal" in output

    def test_a_rerun_proposal_on_a_substrate_class_is_refused(
        self, workspace: Workspace
    ) -> None:
        flagged = "error:NoCredentialsError@aiobotocore/credentials.py:load"
        opened(workspace, flagged, count=40, substrate=True)

        code, output = self.propose("error:NoCredentialsError")

        assert code != 0
        assert "machinery under the run" in output

    def test_an_answer_covers_only_what_still_stands_under_it(
        self, workspace: Workspace
    ) -> None:
        # investigating pulls a class back into the agent's hands; answering
        # the proposal afterwards must not sweep it up with the remainder
        _, output = self.propose("error:TimeoutError", "error:ValueError")
        identifier = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")
        run("investigate", "error:TimeoutError", "--note", "digging")

        code, _ = run(
            "rule", "--proposal", identifier, "--reason", "agreed", "--by", "kaia"
        )

        assert code == 0
        assert [entry["class"] for entry in rulings(workspace)] == [CLASS_B]

    def test_a_recurrence_reproposal_gets_a_fresh_id(
        self, workspace: Workspace
    ) -> None:
        # same classes, same action, next generation: a new question needs a
        # new id, or the appeared-diff and the raised fold treat the item as
        # one somebody was already told about
        _, output = self.propose("error:TimeoutError")
        first = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")
        code, _ = run(
            "rule",
            "--proposal",
            first,
            "--disposition",
            "dismiss",
            "--reason",
            "done",
            "--by",
            "kaia",
        )
        assert code == 0
        opened(workspace, CLASS_A, count=1, refs=["ev2:s9:1:u9"])

        code, output = self.propose("error:TimeoutError")

        assert code == 0
        second = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")
        assert second != first

    def test_a_fully_taken_over_proposal_is_no_longer_answerable(
        self, workspace: Workspace
    ) -> None:
        # every covered class moved on -- one investigated, one superseded
        # into a newer proposal -- so the old id answers nothing
        _, output = self.propose("error:TimeoutError", "error:ValueError")
        stale = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")
        run("investigate", "error:TimeoutError", "--note", "digging")
        self.propose("error:ValueError", action="dismiss")

        code, output = run(
            "rule", "--proposal", stale, "--reason", "agreed", "--by", "kaia"
        )

        assert code != 0
        assert "no live proposal" in output
        assert rulings(workspace) == []


class TestRuleByFinding:
    """Answering a proposal by naming what it is about, never by its id.

    The id is what the fold keys the proposal on; the operator was told about findings and tasks, and the agent should be able to record their answer in those words. The proposal's own disposition is the default, `--disposition` changes one row, and a task stands for everything proposed for it.
    """

    SCAN_A = scan_class(
        "scoring_integrity", "reward_hacking", task="done", identifier=DONE.identifier
    )
    SCAN_B = scan_class(
        "scoring_integrity", "internet_egress", task="done", identifier=DONE.identifier
    )

    def propose(self, *classes: str, action: str = "rerun") -> str:
        code, output = run(
            "propose", *classes, "--action", action, "--reason", "the same cause"
        )
        assert code == 0, output
        return proposal_id(output)

    def test_a_finding_answers_its_proposal_as_proposed(
        self, workspace: Workspace
    ) -> None:
        identifier = self.propose("TimeoutError", "ValueError")

        code, output = run("rule", "TimeoutError", "--reason", "agreed", "--by", "kaia")

        assert code == 0, output
        (landed,) = rulings(workspace)
        assert landed["class"] == CLASS_A
        assert landed["disposition"] == "rerun"
        assert landed["proposal"] == identifier
        assert f"answers {identifier} as proposed" in output
        # the other row is still the operator's question
        code, _ = run(
            "rule", "--proposal", identifier, "--reason", "rest", "--by", "kaia"
        )
        assert code == 0
        assert [entry["class"] for entry in rulings(workspace)] == [CLASS_A, CLASS_B]

    def test_a_disposition_changes_one_row_and_keeps_the_rest_proposed(
        self, workspace: Workspace
    ) -> None:
        identifier = self.propose("TimeoutError", "ValueError")

        code, output = run(
            "rule",
            "TimeoutError",
            "--disposition",
            "exclude",
            "--reason",
            "these are the scorer's fault",
            "--by",
            "kaia",
        )

        assert code == 0, output
        (landed,) = rulings(workspace)
        assert landed["disposition"] == "exclude"
        assert landed["proposal"] == identifier
        assert f"answers {identifier} (proposed rerun)" in output
        # no re-proposal needed: the remainder answers by the same proposal
        code, _ = run(
            "rule", "--proposal", identifier, "--reason", "rest", "--by", "kaia"
        )
        assert code == 0
        assert [entry["disposition"] for entry in rulings(workspace)] == [
            "exclude",
            "rerun",
        ]

    def test_a_task_answers_everything_proposed_for_it(
        self, workspace: Workspace
    ) -> None:
        opened(workspace, self.SCAN_A, count=2, kind="scan", tasks=[DONE.identifier])
        opened(workspace, self.SCAN_B, count=1, kind="scan", tasks=[DONE.identifier])
        identifier = self.propose("reward_hacking", "internet_egress", action="exclude")

        code, output = run(
            "rule", "done", "--reason", "as you proposed", "--by", "kaia"
        )

        assert code == 0, output
        landed = rulings(workspace)
        assert [entry["class"] for entry in landed] == [self.SCAN_B, self.SCAN_A]
        assert all(entry["disposition"] == "exclude" for entry in landed)
        assert all(entry["proposal"] == identifier for entry in landed)
        # the error classes nobody proposed are untouched, even though the
        # task token would have named them had it fanned out to open classes
        assert {CLASS_A, CLASS_B} & {entry["class"] for entry in landed} == set()

    def test_a_task_with_nothing_proposed_is_refused(
        self, workspace: Workspace
    ) -> None:
        code, output = run("rule", "done", "--reason", "r", "--by", "kaia")

        assert code != 0
        assert "nothing is proposed for done" in output
        assert rulings(workspace) == []

    def test_a_finding_nobody_proposed_still_needs_the_disposition(
        self, workspace: Workspace
    ) -> None:
        code, output = run("rule", "TimeoutError", "--reason", "r", "--by", "kaia")

        assert code != 0
        assert "--disposition is required" in output
        assert "TimeoutError errors" in output
        assert rulings(workspace) == []

    def test_a_label_open_on_two_tasks_takes_the_task(
        self, workspace: Workspace
    ) -> None:
        elsewhere = scan_class(
            "scoring_integrity", "reward_hacking", task="other", identifier="other@m"
        )
        opened(workspace, self.SCAN_A, count=2, kind="scan", tasks=[DONE.identifier])
        opened(workspace, elsewhere, count=1, kind="scan", tasks=["other@m"])

        code, output = run(
            "rule",
            "reward_hacking",
            "--disposition",
            "zero",
            "--reason",
            "r",
            "--by",
            "k",
        )
        assert code != 0
        assert "matches 2 classes" in output
        assert "reward hacking in done" in output

        code, output = run(
            "rule",
            "reward_hacking:done",
            "--disposition",
            "zero",
            "--reason",
            "r",
            "--by",
            "k",
        )

        assert code == 0, output
        assert [entry["class"] for entry in rulings(workspace)] == [self.SCAN_A]

    def test_the_proposal_id_still_answers_and_takes_a_prefix(
        self, workspace: Workspace
    ) -> None:
        identifier = self.propose("TimeoutError")

        code, _ = run(
            "rule", "--proposal", identifier[:9], "--reason", "agreed", "--by", "kaia"
        )

        assert code == 0
        assert [entry["proposal"] for entry in rulings(workspace)] == [identifier]

    def test_propose_prints_the_sentence_and_an_id_free_answer(
        self, workspace: Workspace
    ) -> None:
        code, output = run(
            "propose",
            "TimeoutError",
            "ValueError",
            "--action",
            "rerun",
            "--reason",
            "the provider was down",
        )

        assert code == 0, output
        assert (
            "for the operator: 5 samples (TimeoutError, ValueError): the agent "
            "proposes to run them again — the provider was down"
        ) in output
        assert (
            "answer with: steward rule TimeoutError ValueError --reason ..." in output
        )
        assert "--proposal" not in output.split("answer with:")[1]

    def test_propose_json_carries_the_sentence(self, workspace: Workspace) -> None:
        code, output = run(
            "propose", "TimeoutError", "--action", "rerun", "--reason", "r", "--json"
        )

        assert code == 0, output
        payload = json.loads(output)
        assert payload["summary"].startswith("3 samples (TimeoutError): the agent")
        assert payload["answer"] == "steward rule TimeoutError"


class TestInvestigate:
    def test_investigating_lands_the_note(self, workspace: Workspace) -> None:
        code, _ = run(
            "investigate", "error:TimeoutError", "--note", "reading the logs now"
        )

        assert code == 0
        recorded = next(
            event
            for event in read_journal(workspace.journal).events
            if event.type == INVESTIGATING
        )
        assert recorded.payload["class"] == CLASS_A
        assert recorded.payload["note"] == "reading the logs now"

    def test_an_unknown_class_is_refused(self, workspace: Workspace) -> None:
        code, output = run("investigate", "task:", "--note", "n")

        assert code != 0
        assert "no open class matches" in output


class TestAckRefusal:
    def test_an_anomaly_cannot_be_acknowledged(self, workspace: Workspace) -> None:
        code, output = run(
            "ack", "anomaly:TimeoutError", "--reason", "looks fine to me"
        )

        assert code != 0
        assert "closes through a ruling" in output
        assert "steward rule" in output

    def test_a_proposal_items_remediation_takes_the_proposal_form(
        self, workspace: Workspace
    ) -> None:
        # the item's subject is the proposal id, which `steward rule` would
        # parse as a class and refuse -- the pointer must say --proposal
        _, output = run(
            "propose", CLASS_A, "--action", "rerun", "--reason", "transient"
        )
        identifier = next(
            word for word in output.split() if word.startswith("prop-")
        ).rstrip(":")

        code, output = run(
            "ack", f"anomaly:prop:{identifier}", "--reason", "looks fine"
        )

        assert code != 0
        assert f"steward rule --proposal {identifier}" in output


def test_a_ruling_on_a_freshly_detected_class_survives_the_next_tend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # no tend has journaled this window -- only the verb's own status preview
    # has seen it. The verb persists the window before its ruling, so the
    # decision applies instead of the next tend re-opening the class undecided
    create_workspace(tmp_path, git=False)
    probe = SynthTask("probe", samples=3)
    workspace, _ = prepared(tmp_path, [probe])
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/venv/openai/_client.py", line 88, in post\n'
        "    raise APITimeoutError(request=request)\n"
        "openai.APITimeoutError: Request timed out.\n"
    )
    write_log(
        workspace.logs,
        probe,
        completed=2,
        samples=[SynthSample(id="s1", error="APITimeoutError", traceback=traceback)],
    )
    monkeypatch.chdir(workspace.root)

    code, _ = run(
        "rule",
        "error:",
        "--disposition",
        "dismiss",
        "--reason",
        "one-off",
        "--by",
        "kaia",
    )
    assert code == 0

    result = turn(workspace)
    assert result.anomalies.open == ()
    (settled,) = result.anomalies.settled
    assert settled.ruling is not None
    assert settled.ruling.disposition.value == "dismiss"


class TestWhatAnAgentMayDecideAlone:
    """`dismiss` is the agent's; everything that marks the data is an operator's.

    Every scanner false positive used to cost an operator decision, however
    conclusively the agent disproved it — so a run could not reach *nothing
    left to adjudicate* by construction, and the findings that mattered sat in
    a queue beside the ones that did not. `dismiss` is the one disposition that
    marks nothing, which is what makes it safe to hand over: it records that
    somebody looked and there was no case to answer.
    """

    def test_an_agent_may_dismiss_what_it_disproved(self, workspace: Workspace) -> None:
        code, output = run(
            "rule",
            CLASS_A,
            "--disposition",
            "dismiss",
            "--by",
            "agent",
            "--reason",
            "read the grader output; the failing tests are named and real",
        )

        assert code == 0, output
        landed = rulings(workspace)
        assert landed[0]["disposition"] == "dismiss"
        assert landed[0]["by"] == "agent"

    @pytest.mark.parametrize("disposition", ["accept", "exclude", "zero", "score"])
    def test_but_never_one_that_marks_the_data(
        self, disposition: str, workspace: Workspace
    ) -> None:
        # a run certified because a machine ran out of things to flag is the
        # failure the verb exists to prevent
        code, output = run(
            "rule",
            CLASS_A,
            "--disposition",
            disposition,
            "--by",
            "agent",
            "--reason",
            "because",
            "--effect",
            "1 sample",
        )

        assert code != 0
        assert "an operator's decision" in output
        assert rulings(workspace) == []

    def test_nor_a_rerun_it_decided_on_its_own(self, workspace: Workspace) -> None:
        # `rerun` marks nothing either, and is still not the agent's: it spends
        # the account's money and replaces recorded outcomes
        code, output = run(
            "rule", CLASS_A, "--disposition", "rerun", "--by", "agent", "--reason", "x"
        )

        assert code != 0
        assert "an operator's decision" in output
        assert rulings(workspace) == []

    def test_a_person_may_still_dismiss(self, workspace: Workspace) -> None:
        code, output = run(
            "rule", CLASS_A, "--disposition", "dismiss", "--by", "kaia", "--reason", "x"
        )

        assert code == 0, output
        assert rulings(workspace)[0]["by"] == "kaia"
