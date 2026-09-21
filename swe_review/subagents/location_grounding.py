"""Deterministic grounding of review-finding locations (no LLM calls).

Inspired by OCR's comment-resolution chain (own-file resolve, cross-file
relocate, flag), but fully deterministic: verify the path/line a model
reported against the real workspace, relocate by unique basename when the
path is stale, and annotate rather than drop what cannot be grounded.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def extract_location_path(location: Any) -> str:
    """Pull the file path out of a flat "path:line" string or a deep dict."""
    if isinstance(location, str):
        # strip trailing :line / :start-end (but keep Windows drive letters)
        m = re.match(r"^([A-Za-z]:.+?|[^:]+?)(?::\d+)?(?:-\d+)?$", location)
        return m.group(1) if m and m.group(1) else location
    if isinstance(location, dict):
        p = location.get("path", "")
        return p if isinstance(p, str) else ""
    return ""


def _repo_files(repo: Path) -> List[Path]:
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv",
            "DerivedData", "Pods", "build", "dist", ".eggs"}
    return [p for p in repo.rglob("*")
            if p.is_file() and not (set(p.parts) & skip)]


def _resolve(path_str: str, repo: Path, index=None):
    """Return (resolved_relative_path, line_count_or_None, relocated_from).

    relocated_from is set only when the original path was wrong and a unique
    basename match fixed it; None when the original path was fine or the
    basename is ambiguous (left untouched, marked unverified).
    Paths that escape the repo (absolute or .. traversal) are never read from
    disk; they only participate in in-repo basename relocation.

    index: optional mutable dict shared across one report's locations; a
    basename -> [paths] map is built lazily inside it exactly once, so stale
    lookups don't rescan the tree for every location.
    """
    candidate = (repo / path_str).resolve() if path_str else None
    if candidate is not None:
        repo_root = repo.resolve()
        # Containment check: absolute paths and ../.. traversal resolve
        # outside the repo root and are never read from disk.
        if repo_root == candidate or repo_root in candidate.parents:
            if candidate.is_file():
                try:
                    return path_str, len(candidate.read_text(
                        encoding="utf-8", errors="replace").splitlines()), None
                except OSError:
                    return path_str, None, None

    # Stale or escaping path: deterministic relocation by unique basename.
    base = path_str.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not base:
        return None, None, None
    if index is None:
        matches = [p for p in _repo_files(repo) if p.name == base]
    else:
        if "map" not in index:
            index["map"] = {}
            for p in _repo_files(repo):
                index["map"].setdefault(p.name, []).append(p)
        matches = index["map"].get(base, [])
    if len(matches) == 1:
        rel = str(matches[0].relative_to(repo))
        try:
            lines = len(matches[0].read_text(
                encoding="utf-8", errors="replace").splitlines())
        except OSError:
            lines = None
        return rel, lines, path_str
    return None, None, None


def _ground_deep(location: Dict[str, Any], repo: Path, index=None) -> Dict[str, Any]:
    out = dict(location)
    path_str = out.get("path", "")
    if not isinstance(path_str, str) or not path_str:
        out.setdefault("verified", False)
        return out
    resolved, lines, relocated_from = _resolve(path_str, repo, index)
    if resolved is None:
        out["verified"] = False
        out["relocated_from"] = None
        return out
    out["path"] = resolved
    out["relocated_from"] = relocated_from
    out["file_lines"] = lines
    out["verified"] = True
    try:
        start = int(out.get("start_line") or 0)
        end = int(out.get("end_line") or start)
    except (TypeError, ValueError):
        start, end = 0, 0
    out["line_verified"] = bool(lines) and 1 <= start <= lines and 1 <= end <= lines
    return out


def _ground_flat(location: Any, repo: Path, index=None) -> Any:
    if not isinstance(location, str) or ":" not in location:
        return location
    path_str, _, line = location.rpartition(":")
    if not line.isdigit():
        return location
    resolved, lines, relocated_from = _resolve(path_str, repo, index)
    if resolved is None:
        return f"{location} [unverified]"
    tags = ""
    if relocated_from:
        tags = f"relocated_from:{relocated_from}"
    line_ok = bool(lines) and 1 <= int(line) <= lines
    if not relocated_from and line_ok:
        return f"{location} [verified]"
    if not line_ok:
        tags = (tags + ";" if tags else "") + "line-unverified"
    else:
        tags = (tags + ";" if tags else "") + "line-verified"
    return f"{resolved}:{line} [{tags}]"


def ground_report_locations(report, repo_path=None) -> None:
    """Annotate every defect/finding location in place.

    - deep dict locations gain verified / relocated_from / file_lines /
      line_verified keys;
    - flat "path:line" strings gain a [verified]/[unverified]/[relocated_from]
      suffix;
    - a no-op when repo_path is not provided (CLI/scripts without a repo).

    CONTRACT — flat locations are rewritten, not just inspected. This is a
    deliberate, observable schema change: a consumer that parses the location as
    a bare ``path:line`` will see ``path:line [verified]`` after grounding. The
    annotation is applied only to the flat (legacy) shape; use the deep schema
    (``--deep`` / ``to_dict(deep=True)``) when machine-readable location
    metadata is needed.

    Idempotent: a second pass finds no leading-digits line field and returns the
    annotated string unchanged, so grounding twice cannot nest suffixes.
    """
    if not repo_path:
        return
    repo = _safe_repo(repo_path)
    if repo is None:
        return
    index: Dict[str, Any] = {}  # lazily filled once for the whole report
    for entry in list(report.defects) + list(report.findings):
        if isinstance(entry.location, dict):
            entry.location = _ground_deep(entry.location, repo, index)
        else:
            entry.location = _ground_flat(entry.location, repo, index)


def _location_unverified(location: Any) -> bool:
    """True when grounding (or the missing-location path) marked this location
    as not confirmable against the workspace. Best-effort: flat locations that
    never entered grounding (no ``:line`` suffix) carry no marker and are not
    counted."""
    if isinstance(location, dict):
        return location.get("verified") is False or location.get("line_verified") is False
    if isinstance(location, str):
        return "[unverified]" in location or "line-unverified" in location
    return False


def unverified_high_severity(report) -> List[str]:
    """Collect high-severity findings/defects whose locations failed grounding.

    Grounding annotates rather than drops (by design — a stale path can still
    point at a real problem), so without this consumer the verified/unverified
    metadata was invisible downstream. Callers surface the returned entries so
    a human can re-check the small set of unverifiable-but-serious claims
    instead of trusting or rejecting them wholesale.

    Returns entries like "P0 <title> @ <location>".
    """
    out: List[str] = []
    for f in getattr(report, "findings", []) or []:
        if getattr(f, "severity", "") in ("P0", "P1") and _location_unverified(f.location):
            loc = f.location.get("path", "?") if isinstance(f.location, dict) else f.location
            out.append(f"{f.severity} {getattr(f, 'title', '')} @ {loc}")
    for d in getattr(report, "defects", []) or []:
        if getattr(d, "severity", "") == "high" and _location_unverified(d.location):
            loc = d.location.get("path", "?") if isinstance(d.location, dict) else d.location
            desc = (getattr(d, "description", "") or "")[:60]
            out.append(f"high {desc} @ {loc}")
    return out


def _safe_repo(repo_path) -> Optional[Path]:
    p = Path(repo_path)
    return p if p.is_dir() else None
