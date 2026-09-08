"""The scan fold's cadence decision, as a table.

`_fold_due` is the pure heart of the periodic fold: given whether rows are waiting, whether the interval is up, whether a fleet is still writing, and whether a retry episode is open, does this turn fold? The behavioural half — that a real fold then drains the buffer and the read still finds the row — lives in `test_drain.py`; this pins the decision alone, where a synthesized boolean is the whole input.
"""

import pytest
from inspect_steward._tend.turn import _fold_due


@pytest.mark.parametrize(
    ("fold_failing", "buffer_nonempty", "interval_due", "active", "expected"),
    [
        # no rows to fold: nothing to do, whatever the fleet is doing
        (False, False, True, True, False),
        (False, False, True, False, False),
        # rows waiting, interval up, fleet still writing: the ordinary fold
        (False, True, True, True, True),
        # rows waiting, fleet writing, but within the interval: hold off, so
        # the fold is not paid every tend
        (False, True, False, True, False),
        # a quiescent run does not fold, whatever it holds — a settled campaign
        # must not re-compact the buffer, and the terminal finalize folds its
        # tail at signoff
        (False, True, True, False, False),
        (False, True, False, False, False),
        # a retry episode forces a fold regardless of buffer/interval/quiescence,
        # so a fold that failed on the departure turn is never skipped
        (True, False, False, True, True),
        (True, True, False, False, True),
    ],
)
def test_fold_due(
    fold_failing: bool,
    buffer_nonempty: bool,
    interval_due: bool,
    active: bool,
    expected: bool,
) -> None:
    assert (
        _fold_due(
            fold_failing=fold_failing,
            buffer_nonempty=buffer_nonempty,
            interval_due=interval_due,
            active=active,
        )
        is expected
    )
