"""Configured project tools; patch application and execution are never model tools.

The caller authenticates/revalidates the hub identity around every model/tool step.
These tools are local-only because scoped source can still contain private information.
Environment configuration comes from trusted setup, never model-provided paths.
"""
import json
import os
from pathlib import Path

from connectors.workspace import IDENTITY, Workspace, WorkspaceError, _ancestors, _read_file
from skills import Skill, _schema


MAX_RESULT_CHARS = 12_000


class CodingError(RuntimeError):
    """Invalid configuration, identity, or source-tool inputs; messages contain no source."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bounded_text(value, field):
    """Bound the complete serialized result, including JSON escaping and metadata."""
    full_text = value[field]
    value[f"{field}_truncated"] = False
    value[f"original_{field}_characters"] = len(full_text)
    encoded = _json(value)
    if len(encoded) <= MAX_RESULT_CHARS:
        return encoded
    value["truncated"] = True
    value[f"{field}_truncated"] = True
    value[field] = ""
    if len(_json(value)) > MAX_RESULT_CHARS:
        raise CodingError("Source metadata exceeds the tool output limit.")
    low, high = 0, len(full_text)
    while low < high:
        middle = (low + high + 1) // 2
        value[field] = full_text[:middle]
        if len(_json(value)) <= MAX_RESULT_CHARS:
            low = middle
        else:
            high = middle - 1
    value[field] = full_text[:low]
    return _json(value)


def coding_tools(workspace):
    """Return a separate registry; never register apply, execution, roots, or identity tools."""
    if not isinstance(workspace, Workspace):
        raise CodingError("A trusted configured project workspace is required.")

    def read_project_file(path: str, start_line: int = 1, max_lines: int = 100) -> str:
        """Read a bounded UTF-8 source excerpt from the configured project, with full-file SHA-256."""
        if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 200:
            raise CodingError("Use a positive start line and between one and 200 lines.")
        snapshot = workspace.source_snapshot(path)
        lines = snapshot["content"].splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise CodingError("The requested start line is beyond this source file.")
        selected = lines[start_line - 1:start_line - 1 + max_lines]
        result = {key: value for key, value in snapshot.items() if key != "content"}
        result.update(content="".join(selected), start_line=start_line, total_lines=len(lines),
                      selected_end_line=min(start_line - 1 + max_lines, len(lines)),
                      truncated=start_line > 1 or start_line - 1 + max_lines < len(lines))
        return _bounded_text(result, "content")

    def search_project(query: str) -> str:
        """Search permitted project source for literal text and return bounded file/line references."""
        result = {**workspace.search(query), "principal_id": workspace.principal_id, "project_id": workspace.project_id}
        while len(_json(result)) > MAX_RESULT_CHARS and result["matches"]:
            result["matches"].pop()
            result["truncated"] = True
        if len(_json(result)) > MAX_RESULT_CHARS:
            raise CodingError("Search metadata exceeds the tool output limit.")
        return _json(result)

    def propose_project_patch(path: str, expected_sha256: str, new_content: str) -> str:
        """Stage a source replacement for human review; does not apply or execute it. Refresh stale hashes first."""
        result = workspace.prepare_patch(path, expected_sha256, new_content)
        result["truncated"] = False
        return _bounded_text(result, "diff")

    result = {}
    for function in (read_project_file, search_project, propose_project_patch):
        schema = _schema(function)
        schema["function"]["parameters"]["additionalProperties"] = False
        result[function.__name__] = Skill(function, False, schema, local_only=True)
    return result


def _config_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CodingError("Workspace configuration contains duplicate fields.")
        result[key] = value
    return result


def workspace_from_env(identity):
    """Load optional local configuration for this exact authenticated adult/elder principal."""
    configured_path = os.getenv("EDITH_WORKSPACE_CONFIG")
    if not configured_path:
        return None
    if (not isinstance(identity, dict) or not isinstance(identity.get("role"), str)
            or identity["role"] not in {"adult", "elder"}
            or not isinstance(identity.get("principal_id"), str) or not IDENTITY.fullmatch(identity["principal_id"])):
        raise CodingError("Project access requires an authenticated adult or elder identity.")
    try:
        path = Path(configured_path)
        if not path.is_absolute():
            raise CodingError("Workspace configuration must use an absolute local path.")
        _ancestors(path)
        raw, _ = _read_file(path, 16_384)
        value = json.loads(raw, object_pairs_hook=_config_object)
        if (not isinstance(value, dict) or set(value) != {"root", "staging_dir", "principal_id", "project_id"}
                or any(not isinstance(item, str) for item in value.values())):
            raise CodingError("Workspace configuration has invalid fields.")
        if value["principal_id"] != identity["principal_id"]:
            raise CodingError("This project is not assigned to the authenticated principal.")
        return Workspace(value["root"], value["staging_dir"], value["principal_id"], value["project_id"])
    except (OSError, ValueError, TypeError, WorkspaceError):
        raise CodingError("Workspace configuration is invalid or unavailable; project access is disabled.") from None
