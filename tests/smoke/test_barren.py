"""An arm that ran and produced no samples must be named, not hidden by the aggregate count.

The digest reports one `landed` total, so an arm that landed nothing — a credential storm the cap
reaped mid-retry, a sandbox that never started — just fails to add to it and disappears among the
arms that did. Its samples never finalized, so the errored *count* is zero and the ordinary failure
paths say nothing. `landed_by_task` carries the zero per arm so the digest can name it.
"""

from inspect_steward._smoke.digest import (
    Outcome,
    Smoke,
    digest_markdown,
    echo_smoke,
)


def _smoke(**overrides: object) -> Smoke:
    base: dict[str, object] = dict(
        outcome=Outcome.FAILED,
        tasks=3,
        landed=4,
        elapsed=951.0,
        threw=2,
        landed_by_task=(
            ("exploit_gym/mimas", 2),
            ("exploit_gym/mythos-preview", 2),
            ("exploit_gym/mythos-5-1", 0),
        ),
    )
    base.update(overrides)
    return Smoke(**base)  # type: ignore[arg-type]


def test_a_zero_sample_arm_is_named_in_the_digest() -> None:
    body = digest_markdown(_smoke())

    assert "Produced no samples" in body
    assert "`exploit_gym/mythos-5-1`" in body
    # points the reader at where the cause lives, since the eval logs hold none
    assert "worker log" in body
    # the arms that did land samples are not named as barren
    assert "mimas" not in body.split("Produced no samples")[1].split("\n")[0]


def test_it_is_reported_but_does_not_change_the_verdict() -> None:
    # report, not verdict: under a cap a zero can be the deadline, so this never fails the rehearsal
    # on its own — the verdict here is still the scanner throw it was constructed with
    line = digest_markdown(_smoke()).splitlines()[2]
    assert line == "**🛑 not ready to launch — a scanner threw on 2 transcripts**"


def test_the_terminal_account_names_it_too() -> None:
    lines = echo_smoke(_smoke())
    assert any("no samples: exploit_gym/mythos-5-1" in line for line in lines)


def test_a_clean_run_says_nothing_about_barren_arms() -> None:
    body = digest_markdown(
        _smoke(
            outcome=Outcome.PASSED,
            tasks=2,
            landed=4,
            threw=0,
            landed_by_task=(("a", 2), ("b", 2)),
        )
    )
    assert "Produced no samples" not in body


def test_plural_phrasing_when_more_than_one_arm_is_barren() -> None:
    body = digest_markdown(_smoke(landed_by_task=(("a", 2), ("b", 0), ("c", 0))))
    assert "these arms" in body
    assert "`b`" in body and "`c`" in body
