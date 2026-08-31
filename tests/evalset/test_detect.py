import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from inspect_steward._evalset.detect import (
    DefinitionType,
    detect_definition_type,
    install_hint,
)

from ._hawk import requires_hawk

FIXTURES = Path(__file__).parent / "fixtures"

AMBIGUOUS_PY = """
import inspect_flow
from inspect_ai import eval_set
"""

NEITHER_PY = """
print("hello")
"""

NOT_MAPPING_YAML = """
- just
- a
- list
"""

NEITHER_YAML = """
log_dir: logs
not_a_flow_field: true
"""


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("simple_evalset.py", "evalset"),
        ("sweep_evalset.py", "evalset"),
        ("no_eval_set.py", "evalset"),
        ("flow_spec.py", "flow"),
        ("flow_spec.yaml", "flow"),
        pytest.param("hawk_config.yaml", "hawk", marks=requires_hawk),
    ],
)
def test_detect_fixtures(fixture: str, expected: DefinitionType) -> None:
    assert detect_definition_type(FIXTURES / fixture) == expected


@requires_hawk
def test_detect_yaml_ambiguous_when_no_tasks_declared(tmp_path: Path) -> None:
    # an empty task list is valid in every YAML format, so nothing in the
    # document distinguishes them -- detection must ask rather than guess
    path = tmp_path / "empty.yaml"
    path.write_text("tasks: []\n")
    with pytest.raises(ValueError, match="more than one format"):
        detect_definition_type(path)
    assert detect_definition_type(path, "hawk") == "hawk"


def test_detect_yaml_reports_every_format_it_tried(tmp_path: Path) -> None:
    path = tmp_path / "neither.yaml"
    path.write_text(NEITHER_YAML)
    with pytest.raises(ValueError) as ex:
        detect_definition_type(path)
    # a file rejected by the format it was written for must not be diagnosed
    # against only the other one
    assert "flow spec" in str(ex.value)
    assert "hawk eval set config" in str(ex.value)


@pytest.mark.parametrize(
    "filename,content,error_match",
    [
        ("ambiguous.py", AMBIGUOUS_PY, "explicit type"),
        ("neither.py", NEITHER_PY, "no eval_set"),
        ("not_mapping.yaml", NOT_MAPPING_YAML, "YAML mapping"),
        ("malformed.yaml", "tasks: [unclosed\n", "not valid YAML"),
        ("neither.yaml", NEITHER_YAML, "Cannot determine the type"),
        ("unsupported.txt", "anything", "Unsupported definition file"),
    ],
)
def test_detect_errors(
    tmp_path: Path, filename: str, content: str, error_match: str
) -> None:
    path = tmp_path / filename
    path.write_text(content)
    with pytest.raises(ValueError, match=error_match):
        detect_definition_type(path)


def test_detect_explicit_type_wins(tmp_path: Path) -> None:
    path = tmp_path / "ambiguous.py"
    path.write_text(AMBIGUOUS_PY)
    assert detect_definition_type(path, "evalset") == "evalset"
    assert detect_definition_type(path, "flow") == "flow"


def test_detect_explicit_type_extension_mismatch() -> None:
    with pytest.raises(ValueError, match="not compatible"):
        detect_definition_type(FIXTURES / "flow_spec.yaml", "evalset")


def test_detect_module_attribute_usage(tmp_path: Path) -> None:
    path = tmp_path / "attribute.py"
    path.write_text("import inspect_ai\n\ninspect_ai.eval_set(tasks=[], log_dir='x')\n")
    assert detect_definition_type(path) == "evalset"


class _Version(NamedTuple):
    """Stand-in for `sys.version_info`, which `install_hint` reads by attribute."""

    major: int
    minor: int
    micro: int = 0
    releaselevel: str = "final"
    serial: int = 0


@pytest.mark.parametrize(
    ("package", "extra", "version", "advises_install"),
    [
        # below hawk's floor the extra is marker-gated to nothing, so the
        # install command succeeds and changes nothing -- advising it would
        # send someone round in a circle
        ("hawk", "hawk", (3, 12), False),
        ("hawk", "hawk", (3, 13), True),
        ("hawk", "hawk", (3, 14), True),
        # the floor is hawk's alone; nothing else is version-gated
        ("inspect_flow", "flow", (3, 12), True),
    ],
)
def test_install_hint_respects_hawk_python_floor(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    extra: str,
    version: tuple[int, int],
    advises_install: bool,
) -> None:
    monkeypatch.setattr(sys, "version_info", _Version(*version))
    hint = install_hint(package, extra)
    if advises_install:
        assert hint == f"Install it with: pip install inspect_steward[{extra}]"
    else:
        # names the real obstacle, and does not offer installing as the fix
        assert not hint.startswith("Install it with")
        assert "requires Python 3.13" in hint
        assert "this is Python 3.12" in hint
