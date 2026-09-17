"""Structural validation of a unified diff — deterministic, no repository needed.

Why this exists: a model asked to re-emit a large diff very frequently gets the hunk
arithmetic wrong — the `@@ -old,n +new,m @@` counts do not match the body it then
writes. `git apply` rejects that with `corrupt patch at line N`.

That failure mode is expensive out of all proportion to how simple it is, because of
WHERE it surfaces. Observed twice in self-runs of this project's own loop:

    1. `ReviserSubAgent` emits the malformed diff -> status "ok"
    2. `ReviewerSubAgent` reads it as text and APPROVES it (79/100)
    3. `VerifierSubAgent` finally tries `git apply` and rejects it
    4. the loop ends `verification_failed`, having burned two full review passes

Review cannot catch it (it never applies anything), and by the time verify does, no
attempt is left to fix it. Catching it at the point of production — inside the
reviser's parse, where the regen budget can retry — turns a terminal failure into a
recoverable one.

SCOPE — read this before calling it an applicability check. It is NECESSARY BUT NOT
SUFFICIENT: it verifies hunk arithmetic only. `git apply` can still reject a diff that
passes here for reasons that need the work tree (context mismatch, target file
absent, missing `new file mode` header for a created file, path escapes). Conversely,
a diff that fails here can never apply. So this is a cheap PRODUCER-SIDE gate that
catches the observed failure class early; `VerifierSubAgent` remains the authority.
"""

import re
from typing import List, Optional, Tuple

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_GIT_RE = re.compile(r"^diff --git ")


def _declared_counts(match: "re.Match") -> Tuple[int, int]:
    # A missing count means 1 (`@@ -5 +5 @@` is one line, not zero).
    old = int(match.group(2)) if match.group(2) is not None else 1
    new = int(match.group(4)) if match.group(4) is not None else 1
    return old, new


def _is_file_header(lines: List[str], i: int) -> bool:
    """`--- ` alone is ambiguous: a removed line whose content starts with `--`
    renders identically. A real file header is always followed by `+++ `."""
    if _FILE_GIT_RE.match(lines[i]):
        return True
    if lines[i].startswith("--- ") and i + 1 < len(lines):
        return lines[i + 1].startswith("+++ ")
    return False


def validate_unified_diff(pr_diff: str) -> str:
    """Return an empty string when the diff is structurally sound, else the reason.

    Checks the one thing `git apply` refuses on without needing a work tree: that
    every hunk's declared line counts equal the number of body lines. Truncation
    (a hunk claiming more lines than are present) is the same check.
    """
    if not pr_diff or not pr_diff.strip():
        return "empty diff"
    lines = (pr_diff or "").split("\n")
    # `str.split` on text that ends with a newline yields a trailing "" that is NOT
    # a diff line. Counting it as a context line added +1 to both sides of the last
    # hunk — which both rejected valid diffs AND masked a hunk that under-declared
    # its counts by exactly one.
    if lines and lines[-1] == "":
        lines.pop()
    if not any(_HUNK_RE.match(l) for l in lines):
        return "no @@ hunk header found"

    hunks = 0
    i = 0
    while i < len(lines):
        match = _HUNK_RE.match(lines[i])
        if not match:
            i += 1
            continue
        hunks += 1
        old_n, new_n = _declared_counts(match)
        header = lines[i]
        header_line_no = i + 1
        i += 1
        old_seen = new_seen = 0
        while i < len(lines):
            line = lines[i]
            if _HUNK_RE.match(line) or _is_file_header(lines, i):
                break
            if line.startswith("\\"):
                # "\ No newline at end of file" belongs to neither side
                pass
            elif line.startswith("+"):
                new_seen += 1
            elif line.startswith("-"):
                old_seen += 1
            elif line.startswith(" ") or line == "":
                old_seen += 1
                new_seen += 1
            else:
                return (f"malformed hunk body at line {i + 1}: {line[:40]!r} "
                        f"does not start with ' ', '+' or '-'")
            i += 1
        if (old_seen, new_seen) != (old_n, new_n):
            return (f"hunk at line {header_line_no} declares "
                    f"-{old_n} +{new_n} but its body has -{old_seen} +{new_seen} "
                    f"({header[:60]!r})")
    if hunks == 0:
        return "no hunks"
    return ""


def is_applicable_shape(pr_diff: str) -> bool:
    """Convenience predicate for callers that only need the boolean."""
    return validate_unified_diff(pr_diff) == ""
