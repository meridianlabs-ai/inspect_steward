from pathlib import Path

import pytest
from inspect_steward._evalset.detect import DefinitionType, detect_definition_type

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

INVALID_FLOW_YAML = """
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
        ("hawk_config.yaml", "hawk"),
    ],
)
def test_detect_fixtures(fixture: str, expected: DefinitionType) -> None:
    assert detect_definition_type(FIXTURES / fixture) == expected


@pytest.mark.parametrize(
    "filename,content,error_match",
    [
        ("ambiguous.py", AMBIGUOUS_PY, "explicit type"),
        ("neither.py", NEITHER_PY, "no eval_set"),
        ("not_mapping.yaml", NOT_MAPPING_YAML, "YAML mapping"),
        ("invalid_flow.yaml", INVALID_FLOW_YAML, "flow spec"),
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


@pytest.mark.parametrize(
    "fixture,explicit",
    [
        ("simple_evalset.py", "hawk"),
        ("hawk_config.yaml", "evalset"),
    ],
)
def test_detect_explicit_type_extension_mismatch(
    fixture: str, explicit: DefinitionType
) -> None:
    with pytest.raises(ValueError, match="not compatible"):
        detect_definition_type(FIXTURES / fixture, explicit)


def test_detect_module_attribute_usage(tmp_path: Path) -> None:
    path = tmp_path / "attribute.py"
    path.write_text("import inspect_ai\n\ninspect_ai.eval_set(tasks=[], log_dir='x')\n")
    assert detect_definition_type(path) == "evalset"
