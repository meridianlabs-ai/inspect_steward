import ast
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast

import yaml

DefinitionType = Literal["evalset", "flow", "hawk"]

# hawk's own floor. Our extra is marker-gated to match, which means that below
# it `pip install inspect_steward[hawk]` succeeds and installs nothing -- so
# that is the one thing we must not advise there.
_HAWK_PYTHON = (3, 13)


def install_hint(package: str, extra: str) -> str:
    """Explain how to obtain an optional definition package.

    Args:
        package: Import name of the missing package.
        extra: Name of the `inspect_steward` extra that provides it.

    Returns:
        A sentence naming the install command, or why this interpreter cannot have the package.
    """
    if package == "hawk" and sys.version_info < _HAWK_PYTHON:
        have = f"{sys.version_info.major}.{sys.version_info.minor}"
        want = f"{_HAWK_PYTHON[0]}.{_HAWK_PYTHON[1]}"
        return (
            f"hawk requires Python {want} or later, but this is Python {have}, "
            f"where 'pip install inspect_steward[{extra}]' succeeds without "
            "installing anything. Use a newer interpreter to run hawk definitions."
        )
    return f"Install it with: pip install inspect_steward[{extra}]"


_EXTENSIONS: dict[str, list[DefinitionType]] = {
    ".py": ["evalset", "flow"],
    ".yaml": ["flow", "hawk"],
    ".yml": ["flow", "hawk"],
}


def detect_definition_type(
    path: Path, type: DefinitionType | None = None
) -> DefinitionType:
    """Determine the type of an eval set definition file.

    Detection is static — the definition is never executed. Python files are classified by their imports (`inspect_flow` => flow, `eval_set` from `inspect_ai` => evalset). YAML files are validated against an Inspect Flow spec and a Hawk eval set config, and must match exactly one.

    A YAML format can only be tried when its package is installed, so the result is relative to the environment: "exactly one match" means one among the formats that could be checked. This is only observable for a document that declares no tasks, which is the sole shape both formats accept — with both packages present it is reported as ambiguous, with one present it resolves to that one. A document that is genuinely the other format still fails, naming the package that is missing.

    Args:
        path: Path to the definition file.
        type: Explicit definition type (skips detection, but still validated against the file extension).

    Returns:
        The definition type.

    Raises:
        ValueError: If the type cannot be determined (or an explicit `type` is incompatible with the file extension).
    """
    candidates = _EXTENSIONS.get(path.suffix.lower())
    if candidates is None:
        raise ValueError(
            f"Unsupported definition file '{path}': expected a .py file "
            "(eval_set script or flow spec) or a .yaml/.yml file (flow spec)."
        )

    if type is not None:
        if type not in candidates:
            raise ValueError(
                f"Definition type '{type}' is not compatible with "
                f"'{path.suffix}' files (expected one of: {', '.join(candidates)})."
            )
        return type

    if path.suffix.lower() == ".py":
        return _detect_python(path)
    else:
        return _detect_yaml(path)


def _detect_python(path: Path) -> DefinitionType:
    # bytes rather than `read_text()`, which decodes with the locale encoding:
    # `ast.parse` applies Python's own source encoding rules (utf-8, or a PEP
    # 263 coding declaration), which is what actually running the file will do
    tree = ast.parse(path.read_bytes(), filename=str(path))

    imports_flow = False
    references_eval_set = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_module(alias.name, "inspect_flow"):
                    imports_flow = True
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                if _is_module(node.module, "inspect_flow"):
                    imports_flow = True
                if _is_module(node.module, "inspect_ai") and any(
                    alias.name == "eval_set" for alias in node.names
                ):
                    references_eval_set = True
        elif isinstance(node, ast.Attribute):
            # inspect_ai.eval_set(...)
            if (
                node.attr == "eval_set"
                and isinstance(node.value, ast.Name)
                and node.value.id == "inspect_ai"
            ):
                references_eval_set = True

    if imports_flow and references_eval_set:
        raise ValueError(
            f"Cannot determine the type of '{path}': it imports both "
            "inspect_flow and eval_set. Pass an explicit type "
            "('evalset' or 'flow')."
        )
    elif imports_flow:
        return "flow"
    elif references_eval_set:
        return "evalset"
    else:
        raise ValueError(
            f"Cannot determine the type of '{path}': found no eval_set() "
            "import or inspect_flow import. Pass an explicit type if the "
            "usage is indirect."
        )


# YAML definition formats, each validated by its own package's model. Any
# document that names tasks discriminates itself: a flow spec's `{name, model}`
# entries cannot satisfy hawk's `PackageConfig` (which requires `package` and
# `items`), and a hawk config's entries match no member of flow's
# `str | FlowTask | Task` union. Documents that name *no* tasks can satisfy
# both, so requiring exactly one match -- rather than privileging one format --
# is what turns that into a question instead of a silent misread.
_YamlFormat = Literal["flow", "hawk"]


class _YamlFormatSpec(NamedTuple):
    """How to recognize one YAML definition format."""

    type: _YamlFormat
    package: str
    """Top-level package providing the model. Distinct from `module` because `find_spec` raises rather than returning `None` when asked for a submodule of an absent package, and because this is the name to put in an install hint."""
    module: str
    """Module holding the model."""
    model: str
    """Pydantic model defining the format."""
    description: str
    """How to name this format in an error message."""


# The models are named rather than imported, because both packages are
# optional: a static import would leave this module's types unresolvable
# wherever the extra is absent -- for hawk that is every Python 3.12
# environment, since its marker excludes them, and 3.12 is one of the
# interpreters CI typechecks.
_YAML_FORMATS: list[_YamlFormatSpec] = [
    _YamlFormatSpec("flow", "inspect_flow", "inspect_flow", "FlowSpec", "flow spec"),
    _YamlFormatSpec(
        "hawk",
        "hawk",
        # import-light (pydantic only -- inspect_ai is under TYPE_CHECKING), so
        # this costs nothing at detection time
        "hawk.core.types",
        "EvalSetConfig",
        "hawk eval set config",
    ),
]


def _detect_yaml(path: Path) -> DefinitionType:
    try:
        # bytes for the same reason as the Python branch: YAML declares its own
        # encoding (utf-8, or utf-16 by BOM) and PyYAML applies that rule to a
        # bytes input, where `read_text()` would apply the locale's instead
        loaded = yaml.safe_load(path.read_bytes())
    except yaml.YAMLError as ex:
        # a parse error is a `ValueError` to callers like every other bad
        # definition; `yaml.YAMLError` is not one, and would reach the CLI as
        # an unhandled traceback
        raise ValueError(f"Definition file '{path}' is not valid YAML:\n{ex}") from ex
    if not isinstance(loaded, dict):
        raise ValueError(f"Definition file '{path}' does not contain a YAML mapping.")
    data = cast(dict[str, Any], loaded)

    matched: list[_YamlFormat] = []
    rejected: list[str] = []
    uninstalled: list[str] = []
    for format in _YAML_FORMATS:
        if importlib.util.find_spec(format.package) is None:
            uninstalled.append(
                f"{format.description}: {format.package} is not installed. "
                f"{install_hint(format.package, format.type)}"
            )
        elif (error := _validate_yaml(format, data)) is None:
            matched.append(format.type)
        else:
            rejected.append(f"{format.description}:\n{error}")

    if len(matched) == 1:
        # one match *among the formats that could be checked* -- an uninstalled
        # package narrows the field rather than rejecting the document. Only a
        # document declaring no tasks can be read differently as a result, and
        # requiring every package instead would make a flow-only install refuse
        # every flow spec, which is worse than the case it would fix
        return matched[0]
    elif len(matched) > 1:
        # reachable: a document declaring no tasks (`tasks: []`) satisfies
        # every format, since each requires only that the key parse
        raise ValueError(
            f"Cannot determine the type of '{path}': it is valid as more than "
            f"one format ({', '.join(matched)}). A definition that declares no "
            "tasks is the usual cause, because an empty task list is valid in "
            "all of them. Pass an explicit type."
        )
    # nothing matched -- report every reason, so a file rejected by the format
    # it was actually written for isn't diagnosed against the other one
    reasons = "\n\n".join(rejected + uninstalled)
    raise ValueError(f"Cannot determine the type of '{path}':\n\n{reasons}")


def _validate_yaml(format: _YamlFormatSpec, data: dict[str, Any]) -> str | None:
    """Validate parsed YAML against one format's model.

    Only call this once the format's package is known to be installed.

    Args:
        format: Format to validate against.
        data: Parsed YAML mapping.

    Returns:
        `None` if the data is valid for this format, otherwise the validation error.
    """
    from pydantic import BaseModel, ValidationError

    model = cast(
        type[BaseModel],
        getattr(importlib.import_module(format.module), format.model),
    )
    try:
        model.model_validate(data)
        return None
    except ValidationError as ex:
        return str(ex)


def _is_module(name: str, module: str) -> bool:
    return name == module or name.startswith(f"{module}.")
