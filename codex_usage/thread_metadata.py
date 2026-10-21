"""Resolve task labels from saved project metadata and session headers only."""

import json
import ntpath
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pandas as pd


def path_key(value: str) -> str:
    value = value.removeprefix("\\\\?\\")
    if ntpath.splitdrive(value)[0] or "\\" in value:
        return ntpath.normcase(ntpath.normpath(value)).replace("\\", "/").rstrip("/")
    return posixpath.normpath(value).rstrip("/") or "/"


def directory_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "No project"
    path = PureWindowsPath(value) if ntpath.splitdrive(value)[0] or "\\" in value else PurePosixPath(value)
    return path.name or str(path)


def enrich_thread_metadata(
    events: pd.DataFrame, codex_home: Path, session_metadata: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    """Resolve labels before date filtering; refresh project names on every run."""
    result = events.copy()
    if events.empty:
        result["project_name"] = pd.Series(dtype="str")
        result["thread_started_at"] = events["timestamp"].copy()
        return result
    try:
        state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    projects = state.get("local-projects", {})
    projects = projects if isinstance(projects, dict) else {}
    assignments = state.get("thread-project-assignments", {})
    assignments = assignments if isinstance(assignments, dict) else {}
    projectless = state.get("projectless-thread-ids", [])
    projectless = {value for value in projectless if isinstance(value, str)} if isinstance(projectless, list) else set()
    project_names = {}
    root_names = {}
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        name = project.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        project_names[project_id] = name
        roots = project.get("rootPaths", [])
        if isinstance(roots, list):
            for root in roots:
                if isinstance(root, str) and root.strip():
                    root_names[path_key(root)] = name

    names = {}
    starts = {}
    target_tz = events["timestamp"].dt.tz
    for thread_id, group in events.groupby("thread_id", sort=False):
        meta = session_metadata.get(thread_id, {})
        started = pd.to_datetime(meta.get("started_at"), utc=True, errors="coerce")
        starts[thread_id] = started.tz_convert(target_tz) if pd.notna(started) else group["timestamp"].min()
        # Child workspaces must not replace the main thread's project.
        main = group.loc[group["session_id"] == thread_id]
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            candidates = (main if not main.empty else group)["cwd"].dropna()
            cwd = next((value for value in candidates if isinstance(value, str) and value.strip()), "")
        assignment = assignments.get(thread_id, {})
        assigned_name = project_names.get(assignment.get("projectId")) if isinstance(assignment, dict) else None
        if assigned_name:
            name = assigned_name
        elif thread_id in projectless:
            name = "No project"
        else:
            key = path_key(cwd) if cwd else ""
            matches = [root for root in root_names if key == root or key.startswith(root.rstrip("/") + "/")]
            name = root_names[max(matches, key=len)] if matches else directory_name(cwd)
        names[thread_id] = name
    result["project_name"] = result["thread_id"].map(names)
    result["thread_started_at"] = result["thread_id"].map(starts)
    return result
