import ast
import importlib.util
from pathlib import Path
from typing import Any, Literal, cast

import yaml

DefinitionType = Literal["evalset", "flow"]

_EXTENSIONS: dict[str, list[DefinitionType]] = {
    ".py": ["evalset", "flow"],
    ".yaml": ["flow"],
    ".yml": ["flow"],
}


def detect_definition_type(
    path: Path, type: DefinitionType | None = None
) -> DefinitionType:
    """Determine the type of an eval set definition file.

    Detection is static — the definition is never executed. Python files are classified by their imports (`inspect_flow` => flow, `eval_set` from `inspect_ai` => evalset). YAML files must validate as an Inspect Flow spec (which requires `inspect_flow` to be installed).

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
    tree = ast.parse(path.read_text(), filename=str(path))

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


def _detect_yaml(path: Path) -> DefinitionType:
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        raise ValueError(f"Definition file '{path}' does not contain a YAML mapping.")
    data = cast(dict[str, Any], loaded)

    if importlib.util.find_spec("inspect_flow") is not None:
        from inspect_flow import FlowSpec
        from pydantic import ValidationError

        try:
            FlowSpec.model_validate(data)
            return "flow"
        except ValidationError as ex:
            raise ValueError(
                f"Definition file '{path}' is not a valid flow spec:\n{ex}"
            ) from ex
    else:
        raise ValueError(
            f"Cannot determine the type of '{path}': inspect_flow is not "
            "installed to validate it as a flow spec. Install with "
            "'pip install inspect_steward[flow]' or pass an explicit type."
        )


def _is_module(name: str, module: str) -> bool:
    return name == module or name.startswith(f"{module}.")
