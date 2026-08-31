"""Skip marker for tests that need the `hawk` extra.

Hawk requires Python 3.13 and inspect_steward does not, so the extra is
marker-gated and simply absent on 3.12 — where these tests skip rather than
fail. Not named `test_*`, so pytest does not collect it.
"""

from importlib.util import find_spec

import pytest

requires_hawk = pytest.mark.skipif(
    find_spec("hawk") is None,
    reason="the hawk extra is not installed (it requires Python 3.13+)",
)
