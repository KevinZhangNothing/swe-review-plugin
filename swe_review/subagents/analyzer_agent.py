"""
Analyzer SubAgent - 静态分析

为 Reviewer / Generator 提供二级信号：
- diff hunk 复杂度（行数 / 嵌套 / 函数嵌套）
- 怀疑的回归点（修改的 public API）
- 调用影响范围粗算
- 反臃肿信号（flag 堆积 / 分支叠加 / 只加不删 / 小改动加包装层）

不读 LLM，纯规则。
"""

import re
from collections import Counter
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict


@dataclass
class AnalyzerResult:
    total_hunks: int
    total_additions: int
    total_deletions: int
    files_changed: List[str]
    public_api_changes: List[str]
    suspicious_spots: List[Dict[str, str]]
    complexity_score: float
    # Copy-paste redundancy signal: identical added lines repeated across
    # multiple hunks/files — evidence for "extract a common function instead
    # of duplicating". Fed to the reviewer inside Patch Analysis.
    repeated_added_blocks: List[Dict[str, Any]] = field(default_factory=list)
    # Anti-bloat signals (flag accumulation, guard stacking, additive-only
    # change, compat layer, wrapper growth) — static evidence for "this change
    # stacks new state/branches instead of simplifying the existing logic".
    bloat_signals: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Lines that repeat for idiomatic reasons, not because of copy-paste
# redundancy: imports, comments, bare closers/keywords.
_REDUNDANCY_SKIP_RE = re.compile(
    r"^(import |from |#|//|/\*|\*|\"\"\"|'''"
    r"|[}\])]+;?$"
    r"|return$|raise$|pass$|continue$|break$|else:?$|try:?$)"
)


#: Paths that hold tests rather than shipped code. A new `def test_*` or fixture
#: is not a change to the product's public surface; counting it both misleads the
#: reviewer and saturates the API term of `complexity_score` — API_NORMALIZER is
#: 5, so five added test functions alone push that term to 1.0.
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|spec)/"
    r"|(^|/)test_[^/]*$"
    r"|(^|/)[^/]*_test\.[A-Za-z0-9]+$"
    r"|\.(test|spec)\.[A-Za-z0-9]+$"
)


#: Directories whose contents are produced by a build/sync step and committed as
#: byte-identical copies. A line repeating between a source and its mirror is not
#: copy-pasted logic. Kept separate from `_NON_CODE_PATH_RE` because these hold
#: real code too — only the copy-paste signal is meaningless for them.
_GENERATED_PATH_RE = re.compile(
    r"(^|/)(?:\.claude_skills|build|dist|node_modules|\.venv|__pycache__)/"
    r"|\.min\.(?:js|css)$"
)


def _is_generated_path(path: str) -> bool:
    """True when a path is machine-produced output or a mirror of one."""
    return bool(path) and bool(_GENERATED_PATH_RE.search(path))


def detect_repeated_added_blocks(
    pr_diff: str,
    min_line_len: int = 12,
    min_count: int = 2,
    cap: int = 10,
) -> List[Dict[str, Any]]:
    """Static redundancy signal: identical added lines repeated across the diff.

    Reports added lines that occur >= `min_count` times in >=2 distinct
    (file, hunk) locations — i.e. copy-pasted logic that should likely be
    extracted into a shared/common function or reuse an existing one. This is
    EVIDENCE for the reviewer, not a verdict: short/idiomatic lines are
    filtered out to keep noise low.

    Returns a list capped at `cap` entries (most repeated first):
      {"line": str, "count": int, "files": [..], "hunks": int}
    """
    counts: Counter = Counter()
    locations: Dict[str, set] = {}
    current_file = ""
    hunk_idx = 0
    for line in pr_diff.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            current_file = (
                parts[2].removeprefix("a/").removeprefix("b/")
                if len(parts) >= 3 else ""
            )
            continue
        if line.startswith("@@"):
            hunk_idx += 1
            continue
        if line.startswith("+++") or not line.startswith("+"):
            continue
        # Generated mirrors are byte-identical copies by design, so every line in
        # them repeats — that is the mirror doing its job, not copy-pasted logic.
        # Seen in this project's own self-review: 10/10 repeated blocks were
        # `.claude/skills/…/SKILL.md` vs its `swe_review/.claude_skills/` mirror.
        if _is_generated_path(current_file):
            continue
        text = line[1:].strip()
        if len(text) < min_line_len or _REDUNDANCY_SKIP_RE.match(text):
            continue
        counts[text] += 1
        locations.setdefault(text, set()).add((current_file, hunk_idx))

    out: List[Dict[str, Any]] = []
    for text, cnt in counts.most_common():
        if cnt < min_count:
            break
        locs = locations[text]
        # Require repetition ACROSS hunks/files; repeats inside a single hunk
        # are too likely to be incidental (test assertions, similar branches).
        if len(locs) < 2:
            continue
        out.append({
            "line": text[:120],
            "count": cnt,
            "files": sorted({f for f, _ in locs if f})[:5],
            "hunks": len(locs),
        })
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Anti-bloat signals — static evidence for ADDITIVE problem solving
# ---------------------------------------------------------------------------
# The failure mode these target is "patch stacking": a change that resolves a
# local symptom by ADDING a bool, a flag, a guard, a compatibility branch, or a
# wrapper layer, instead of adjusting the logic that is already there. The cost
# is invisible per change and compounds across changes.
#
# None of these signals is a verdict. Each one is countable evidence handed to
# the reviewer, and the thresholds sit deliberately ABOVE one: adding one
# necessary variable or one guard is normal engineering, so reporting it would
# be exactly the low-value noise the review prompt forbids.

#: A declared boolean-ish state variable (`self.done = False`, `let ok = true`,
#: `is_open: bool = True`). The NAME must also look like a flag — `x = 0` or a
#: literal assignment to `result` is not a state flag.
_FLAG_DECL_RE = re.compile(
    r"^[ \t]*(?:self\.|this\.|cls\.)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*"
    r"(?::[ \t]*(?:bool|boolean))?[ \t]*=[ \t]*"
    r"(?:True|False|true|false|None|null)[ \t]*$"
)
_FLAG_NAME_RE = re.compile(
    r"(?:^|_)(?:is|has|should|can|could|allow|enable|enabled|disable|skip|use)_"
    r"|(?:^|_)(?:flag|flags|enabled|disabled|mode)$",
    re.IGNORECASE,
)

#: An added conditional / guard. `\}\s*` tolerates brace-style closers.
_CONDITION_RE = re.compile(
    r"^[ \t]*(?:\}[ \t]*)?(?:if|elif|else\s+if|else|switch|case|when|unless|guard)\b"
)

#: Markers of a compatibility / safety-net layer added on top of old logic.
#: Underscore counts as a separator (NOT a word character): the most common real
#: spelling is a compound name (`legacy_mode`, `compat_shim`, `fallback_path`),
#: which `\b`-based matching silently missed.
#: The capture group is what makes `compat_layer` mean "several DIFFERENT
#: compatibility mechanisms coexist". Counting raw line hits instead made the
#: detector report itself: `_COMPAT_RE.search`, `compat.append`, `len(compat)` are
#: five occurrences of ONE concept, not a layer stack.
_COMPAT_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(fallback|legacy|compat|compatible|backward"
    r"|deprecated|workaround|graceful|defensive)(?:[^A-Za-z0-9]|$)",
    re.IGNORECASE,
)

#: A line that DEFINES a symbol rather than uses it (`_COMPAT_RE = re.compile(...)`).
#: Such a line merely names the concept; counting it made this detector report
#: `compat_layer` against its own definition during self-review.
_SYMBOL_DEF_RE = re.compile(r"^[ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*(?::[^=]+)?=(?!=)")

def _indent_of(text: str) -> int:
    """Leading-whitespace width, used to tell "inside the new def" from "outside"."""
    return len(text) - len(text.lstrip())

#: A line that is entirely a comment in any of the supported syntaxes.
_COMMENT_ONLY_RE = re.compile(r"^[ \t]*(?:#|//|/\*|\*|<!--)")

#: A line that is entirely a string/block-literal line (a prompt, a message).
_LITERAL_LINE_RE = re.compile(r"""^[ \t]*(?:[rbuf]{0,2})(?:["']|\"\"\"|''')""")

#: `@@ -0,0 +1,N @@` — the hunk that creates a brand-new file. Additions in a
#: new file carry no deletions by definition, so they must never be read as
#: "additive-only change".
_NEW_FILE_HUNK_RE = re.compile(r"^@@ -0,0 ")
_TOP_LEVEL_DEF_RE = re.compile(r"^(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_][A-Za-z0-9_]*)")

#: The closing/continuation lines of a multi-line def signature: `) -> R:`,
#: `):`, `) {`, or a bare `)` , plus the parameter lines themselves.
_SIGNATURE_CONTINUATION_RE = re.compile(
    r"^[ \t]*\)[ \t]*(?:->[ \t]*[^:]+)?[ \t]*:?[ \t]*\{?[ \t]*$"
    r"|^[ \t]*(?:async[ \t]+)?(?:def|class)[ \t]+\S*\([ \t]*$"
)

_TRIPLE_QUOTES = ('"""', "'''")


def _scan_literal(text: str, inside: bool) -> Tuple[bool, bool]:
    """Advance block-literal state by one line.

    Returns ``(is_prose, new_state)``. The first element answers "is this line
    itself prose?" — either it is already inside a block, or it opens one here
    (the opener line carries prose too). The second is the state the next line
    starts in.

    Odd triple-quote count crosses a boundary (opens or closes a block), so the
    state flips; even count leaves it unchanged. Tracking this on CONTEXT lines
    as well as added ones is load-bearing: a docstring is usually opened by an
    unchanged line, and advancing only on `+` lines desynchronised the state so
    later prose got matched as code.
    """
    trips = sum(text.count(q) for q in _TRIPLE_QUOTES)
    if trips % 2:
        return True, not inside
    return inside, inside


def _is_prose_line(text: str) -> bool:
    """True when a line is documentation/prompt text rather than code.

    Kept as one predicate instead of inline conditions at each call site: the
    signal loop needs a single, testable answer to "does this line execute?".
    """
    return bool(_COMMENT_ONLY_RE.match(text) or _LITERAL_LINE_RE.match(text))

#: Added/removed lines that are pure structure — counting them as evidence of
#: "new logic" overstates every signal. Each branch MUST require at least one
#: significant character: an earlier form allowed the closer set to match the
#: empty string, which classified every added line as structural and silently
#: disabled `additive_only_change`. Comments are NOT handled here — they are
#: prose, and `_is_prose_line` owns that judgement in one place.
_STRUCTURAL_ONLY_RE = re.compile(
    r"^[ \t]*(?:"
    r"[}\]\)]+[;,]?[ \t]*(?:(?:#|//).*)?$"
    r"|(?:pass|continue|break|else|try|finally)\b[ \t]*:?[ \t]*$"
    r")"
)


def _effective_added(text: str) -> bool:
    """True when an added line carries real logic (not a closer/comment)."""
    stripped = text.strip()
    if not stripped or _STRUCTURAL_ONLY_RE.match(text):
        return False
    return len(stripped) >= 4


# ---------------------------------------------------------------------------
# Field-list duplication — the "same field list maintained in N places" defect
# ---------------------------------------------------------------------------
# Why this needs its own mechanism: the five line-pattern signals above are
# blind to it. A new model field and the mapper entry that serializes it are
# DIFFERENT text, so `detect_repeated_added_blocks` (which requires identical
# lines) sees nothing, and no per-line pattern can tell that
# `final bool stereoCharging;` and `'stereoCharging': o.stereoCharging,` are the
# same fact written twice. When they drift, a field silently stops being
# serialized — a regression with no compile error and often no test.
#
# The judgement is deliberately NOT "did >=2 fields get added": adding two
# fields to ONE new struct is ordinary work. It is "is the same SET of field
# names declared in one place and enumerated in another" — i.e. the list is
# duplicated, so a future field addition must be remembered in both.

#: A field DECLARATION: `final bool stereoCharging;`, `int? count,`,
#: `private String id = "";`, `public int Age { get; set; }`.
#: Group 1 = field name. Ordered so `final`/`required` modifiers cannot be
#: mistaken for the type.
_FIELD_DECL_RE = re.compile(
    r"^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]*)*"
    r"(?:(?:public|private|protected|internal|static|final|readonly|const"
    r"|late|required|mutable|var|val|let)[ \t]+)*"
    r"([A-Za-z_][\w<>,\.\]\?\[\] ]*?)[ \t]+"
    r"([A-Za-z_]\w*)[ \t]*(?:=[^;]*)?[;,][ \t]*$"
)

#: A map/serialization KEY: `'stereoCharging': o.stereoCharging,`,
#: `"providerId": id,`, `stereoCharging: value,` (JS/YAML-ish).
_MAP_KEY_RE = re.compile(
    r"""^[ \t]*(?:['"]([A-Za-z_]\w*)['"]|([A-Za-z_]\w*))[ \t]*:[ \t]*\S"""
)

#: An ANNOTATED field: `stereo_charging: bool`, `provider_id: str = ""`.
#: Covers Python (dataclass/pydantic/TypedDict), TypeScript `readonly x: T`,
#: and Kotlin `val x: T`. Needed as a SEPARATE pattern because the name/type
#: order is reversed relative to `final bool x;` and the line need not end in
#: `;` or `,` — the shape that made Python models invisible to the first pass.
_ANNOTATED_FIELD_RE = re.compile(
    r"^[ \t]*(?:(?:readonly|public|private|protected|final|late)[ \t]+)*"
    r"([A-Za-z_]\w*)[ \t]*:[ \t]*"
    r"([A-Za-z_][\w<>\[\]\.\, \|\?]*)"
    r"(?:[ \t]*=[ \t]*.*)?$"
)

#: Keywords whose "declaration" shape is not a field (`if x: y`, `case a: b`).
_NOT_A_FIELD_RE = re.compile(
    r"^(?:if|elif|else|for|while|return|case|switch|when|match|import|from"
    r"|class|def|function|type|interface|enum|namespace|using|export)$"
)


def _field_name(text: str) -> str:
    """Field name from a declaration line, or "" when it is not one."""
    # `final bool x;` / `int? count,` (type-first)
    m = _FIELD_DECL_RE.match(text)
    if m:
        name = m.group(2)
        if _NOT_A_FIELD_RE.match(name) or " " in name or not name.isidentifier():
            return ""
        return name
    # `x: bool` (name-first, annotated)
    m = _ANNOTATED_FIELD_RE.match(text)
    if m:
        name = m.group(1)
        # A bare `key: value,` inside a dict literal matches this shape too, so
        # the value must look like a TYPE, never like a reference (`.`/`(`).
        type_part = m.group(2).strip()
        if (_NOT_A_FIELD_RE.match(name) or not name.isidentifier()
                or not type_part or type_part[0] in "'\""
                or "." in type_part.split("[")[0].replace("Self", "")
                or "(" in type_part):
            return ""
        return name
    return ""


def _map_key(text: str) -> str:
    """Serialization key from a `'key': value` line, or "" when not one."""
    m = _MAP_KEY_RE.match(text)
    if not m:
        return ""
    key = m.group(1) or m.group(2)
    if _NOT_A_FIELD_RE.match(key):
        return ""
    return key


def detect_field_list_duplication(
    pr_diff: str, min_shared: int = 2
) -> List[Dict[str, Any]]:
    """Report when the same field-name set is declared AND enumerated elsewhere.

    Fires only when an added field DECLARATION and an added serialization KEY
    share >= `min_shared` names across two different files. That co-occurrence
    is what makes the list duplicated: the field must now be added in both
    places or it silently stops round-tripping.

    Deliberately conservative:
    - One new struct with several fields and no map counterpart produces no
      signal (nothing is duplicated).
    - A mapper that renames keys (`providerId` -> `provider_id`) shares no
      exact name and produces no signal. That is a KNOWN LIMIT of a
      text-level check — see the module note; a type-aware check would need
      real symbol resolution. Staying silent beats a wrong claim.
    """
    decls: Dict[str, Dict[str, set]] = {}   # file -> {name -> set of lines}
    maps: Dict[str, Dict[str, set]] = {}

    current_file = ""
    for line in (pr_diff or "").split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            current_file = (
                parts[2].removeprefix("a/").removeprefix("b/")
                if len(parts) >= 3 else ""
            )
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if not current_file or not _is_code_path(current_file):
            continue
        text = line[1:]

        name = _field_name(text)
        if name:
            decls.setdefault(current_file, {}).setdefault(name, set()).add(text.strip())
            continue

        key = _map_key(text)
        if key:
            maps.setdefault(current_file, {}).setdefault(key, set()).add(text.strip())

    if not decls or not maps:
        return []

    out: List[Dict[str, Any]] = []
    for dfile, dnames in decls.items():
        for mfile, mnames in maps.items():
            if dfile == mfile:
                # Same file (e.g. a fromMap next to the class) is far more often
                # a legitimate co-located DTO than a duplicated list across the
                # codebase; requiring two files keeps the signal specific.
                continue
            shared = sorted(set(dnames) & set(mnames))
            if len(shared) < min_shared:
                continue
            out.append({
                "kind": "field_list_duplication",
                "count": len(shared),
                "files": [dfile, mfile],
                "evidence": [f"{dfile}: {sorted(dnames)[0]} (declaration)",
                             f"{mfile}: {sorted(mnames)[0]} (mapping)",
                             f"shared fields: {', '.join(shared[:6])}"],
                "note": ("同一组字段在『声明处』与『映射处』各维护一份；"
                         "新增字段时必须两处同步，漏一处即是静默回归"
                         "（字段不再被序列化/反序列化）。确认能否由单一清单驱动。"),
            })
            break  # one declaration/mapping pair is enough per declaration file
    return out


#: Files whose diff content is prose/config, not executable logic. Bloat signals
#: judge whether CODE grew by stacking state and branches; a README section or a
#: SKILL.md rule that happens to contain the word "fallback" is not evidence of
#: anything. Observed directly: documenting these very signals inside
#: engineering_prompt.py made the detector report `compat_layer` against the
#: prompt text itself.
_NON_CODE_PATH_RE = re.compile(
    r"\.(?:md|markdown|rst|txt|adoc)$"
    r"|(?:^|/)(?:README|CHANGELOG|LICENSE|NOTICE)(?:\.[A-Za-z0-9]+)?$"
    r"|\.(?:json|ya?ml|toml|ini|cfg|lock)$",
    re.IGNORECASE,
)


def _is_code_path(path: str) -> bool:
    """True when a path holds executable product logic worth bloat-judging.

    Excludes prose/config (see `_NON_CODE_PATH_RE`) and tests (reuses
    `_TEST_PATH_RE`): a test named `test_compat_layer_detected` asserts ABOUT a
    concept rather than being an instance of it, and test files are legitimately
    additive and branch-heavy.
    """
    if not path:
        return False
    return not (_NON_CODE_PATH_RE.search(path) or _TEST_PATH_RE.search(path))


def _scan_diff_signals(pr_diff: str) -> Dict[str, Any]:
    """Phase 1 — walk the diff once and collect raw per-line evidence.

    Split out of `detect_bloat_signals`, which carried both phases (scan +
    threshold) in ~160 lines of mixed state. They answer different questions:
    this one is "what did the diff literally contain", the aggregator is
    "which of those clears a threshold". Separating them is what makes the
    thresholds reviewable without first reading the parser.
    """
    flags: List[Dict[str, str]] = []
    compat: List[Dict[str, str]] = []
    wrappers: List[Dict[str, str]] = []
    guard_added: List[Dict[str, str]] = []
    #: file -> {"add": int, "del": int, "new_file": bool}
    per_file: Dict[str, Dict[str, Any]] = {}

    current_file = ""
    in_new_file = False
    #: True while inside a multi-line string (docstring / block literal). Text in
    #: a docstring DESCRIBES behaviour; it is not behaviour, so it must not be
    #: matched against the anti-bloat patterns.
    in_docstring = False
    #: Conditional lines DELETED anywhere in the diff (a rewritten branch means
    #: the old condition was removed, so stacking did not happen).
    guard_removed = 0
    #: Indentation of the body of a function/class this diff CREATES, or None.
    #: Guards inside a brand-new function are that function's internal logic, not
    #: branches stacked onto existing code — counting them made "add one
    #: self-contained helper" report as guard_stacking + wrapper_growth.
    new_def_indent: Optional[int] = None
    #: Once inside a created def, a blank line must NOT close it: a top-level
    #: `def` sits at indent 0, and the blank line right after it would otherwise
    #: compare equal and reset the scope immediately.
    in_new_def = False

    for line in (pr_diff or "").split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            current_file = (
                parts[2].removeprefix("a/").removeprefix("b/")
                if len(parts) >= 3 else ""
            )
            in_new_file = False
            per_file.setdefault(current_file, {"add": 0, "del": 0, "new_file": False})
            continue
        if line.startswith("@@"):
            in_new_file = bool(_NEW_FILE_HUNK_RE.match(line))
            if current_file and in_new_file:
                per_file.setdefault(current_file, {"add": 0, "del": 0,
                                                   "new_file": False})["new_file"] = True
            continue
        if line.startswith(("+++", "---", "index ", "new file mode", "\\ ")):
            continue
        if not current_file:
            continue
        # Prose/config never produces code-bloat evidence (see _NON_CODE_PATH_RE).
        if not _is_code_path(current_file):
            continue
        stats = per_file.setdefault(current_file,
                                    {"add": 0, "del": 0, "new_file": False})

        # Literal state must advance on CONTEXT lines too, not just added ones — see
        # _scan_literal. `opens_here` marks the line that opens a block.
        opens_here, in_docstring = _scan_literal(line[1:], in_docstring)

        if line.startswith("+"):
            text = line[1:]
            # Prose is not growth. A change that only appends comments deleted
            # nothing, but it also ADDED no logic — counting it produced an
            # `additive_only_change` on every large documentation pass, the
            # most frequent false positive of this detector.
            is_prose = opens_here or _is_prose_line(text)
            if not is_prose and _effective_added(text):
                stats["add"] += 1
            body = text.strip()

            # Text that MENTIONS a keyword is not code that USES it: comments,
            # docstrings and block literals describe behaviour rather than being
            # behaviour. Skipping them fixes the self-referential false positives
            # this detector produced against its own source.
            if is_prose:
                continue

            # Flag match comes BEFORE the symbol-definition skip: `is_ready =
            # False` is both a "definition" and a state flag, and skipping it
            # first silently disabled flag_accumulation for every LOCAL flag —
            # i.e. exactly the pattern this check exists to catch.
            m = _FLAG_DECL_RE.match(text)
            if m and _FLAG_NAME_RE.search(m.group(1)):
                flags.append({"file": current_file, "line": body[:100]})
                continue

            if _SYMBOL_DEF_RE.match(text):
                continue

            # Enter/leave the body of a function or class this diff CREATES. A blank line
            # never closes the scope (see in_new_def); a non-blank line dedented back
            # to or past the def does — EXCEPT a continuation of that def's own
            # signature, which is what a multi-line parameter list looks like:
            #     def f(
            #         a, b,
            #     ) -> R:            <- indent 0, but still the same def
            # Treating the closer as a new statement ended the scope there, so
            # every guard in the new function was reported as stacking onto
            # existing code — seen when this very detector was added.
            indent = _indent_of(text)
            continues_signature = bool(
                _SIGNATURE_CONTINUATION_RE.match(text)
                or (in_new_def and body.rstrip().endswith((",", "(")))
            )
            if _TOP_LEVEL_DEF_RE.match(text):
                new_def_indent, in_new_def = indent, True
            elif (in_new_def and body and indent <= new_def_indent
                  and not continues_signature):
                new_def_indent, in_new_def = None, False

            if _CONDITION_RE.match(text):
                # A guard inside a brand-new function is that function's own
                # logic, not a branch stacked onto existing code.
                guard_added.append({
                    "file": current_file, "line": body[:100],
                    "in_new_def": in_new_def,
                })

            cm = _COMPAT_RE.search(text)
            if cm:
                compat.append({"file": current_file, "line": body[:100],
                               "concept": cm.group(1).lower()})

            dm = _TOP_LEVEL_DEF_RE.match(text)
            if dm:
                wrappers.append({"file": current_file, "name": dm.group(1)})
        elif line.startswith("-"):
            text = line[1:]
            if _effective_added(text):
                stats["del"] += 1
            if _CONDITION_RE.match(text):
                guard_removed += 1

    return {
        "flags": flags,
        "compat": compat,
        "wrappers": wrappers,
        "guard_added": guard_added,
        "guard_removed": guard_removed,
        "per_file": per_file,
    }


def detect_bloat_signals(pr_diff: str, cap_per_kind: int = 6) -> List[Dict[str, Any]]:
    """Return static anti-bloat signals found in a unified diff.

    Each entry is evidence, shaped for the reviewer's Patch Analysis:
      {"kind", "count", "files", "evidence", "note"}

    Kinds (all thresholds deliberately above one — a single variable or guard is
    normal engineering and must not be reported):

    - ``additive_only_change`` — an EXISTING file gained >= 8 real lines and lost
      none. The signature of "solve it by adding, never by adjusting".
    - ``flag_accumulation``   — >= 2 new boolean-ish state variables. Two flags
      for one local feature is the "one more bool to control it" spiral.
    - ``guard_stacking``      — >= 3 added conditionals with no conditional
      removed. Branches pile up instead of the existing branch being rewritten.
    - ``compat_layer``        — >= 2 added compatibility/safety-net markers
      (fallback / legacy / workaround …) layered on old logic.
    - ``wrapper_growth``      — a small additive change to an EXISTING file that
      introduces a new top-level def/class. "Simple need → new manager".

    Empty list means no signal — never a statement that the change is clean.
    """
    scanned = _scan_diff_signals(pr_diff)
    flags = scanned["flags"]
    compat = scanned["compat"]
    wrappers = scanned["wrappers"]
    guard_added = scanned["guard_added"]
    guard_removed = scanned["guard_removed"]
    per_file = scanned["per_file"]

    out: List[Dict[str, Any]] = []

    def _add(kind: str, count: int, files: List[str], evidence: List[str],
             note: str) -> None:
        out.append({
            "kind": kind,
            "count": count,
            "files": sorted({f for f in files if f})[:5],
            "evidence": evidence[:cap_per_kind],
            "note": note,
        })

    # 1) Existing files that only grow. Two shapes count, and the second was
    # missing: splitting 30 added lines across 4 files (7 each) is still "add
    # without adjusting", but no single file reaches the per-file threshold.
    existing = {p: s for p, s in per_file.items() if not s["new_file"]}
    growing = [p for p, s in existing.items() if not s["del"] and s["add"] >= 8]
    if growing:
        _add("additive_only_change", existing[growing[0]]["add"], [growing[0]],
             [f"+{existing[growing[0]]['add']} lines, -0 lines in {growing[0]}"],
             "解决方式只有新增、没有调整既有逻辑；确认这是必要新增还是叠加式修补。")
    else:
        total_growth = sum(s["add"] for s in existing.values())
        total_loss = sum(s["del"] for s in existing.values())
        if total_growth >= 8 and total_loss == 0:
            _add("additive_only_change", total_growth, list(existing),
                 [f"+{total_growth} lines, -0 lines across "
                  f"{len(existing)} existing file(s)"],
                 "跨文件合计只有新增、没有调整既有逻辑；确认这是必要新增还是叠加式修补。")

    # 2) Boolean/flag state piling up.
    if len(flags) >= 2:
        _add("flag_accumulation", len(flags), [f["file"] for f in flags],
             [f"{f['file']}: {f['line']}" for f in flags],
             "本次新增多个布尔状态；确认它们是否都能由已有数据推导，或可合并为一个状态。")

    # 3) Guards piling up without rewriting the existing branch. Guards that live
    # inside a function this change CREATES are excluded: they are that
    # function's own logic, not branches layered onto existing code.
    stacked = [g for g in guard_added if not g["in_new_def"]]
    if len(stacked) >= 3 and guard_removed == 0:
        _add("guard_stacking", len(stacked), [g["file"] for g in stacked],
             [f"{g['file']}: {g['line']}" for g in stacked],
             "只新增条件分支且未改写既有分支；确认新老逻辑是否应合并统一，而不是层层叠加。")

    # 4) Compatibility / safety-net layer over old logic. Requires >= 2 DIFFERENT
    # mechanisms: one concept used repeatedly (`compat.append`, `len(compat)`) is
    # a single mechanism, not a layer stack — that was the self-report noise.
    compat_concepts = {f["concept"] for f in compat}
    if len(compat_concepts) >= 2:
        _add("compat_layer", len(compat), [f["file"] for f in compat],
             [f"{f['file']}: {f['line']}" for f in compat],
             "存在兼容/兜底层；确认旧路径是否已无用、能否删除旧逻辑而不是并存两条。")

    # 5) New top-level abstraction inside a small additive change.
    total_add = sum(s["add"] for s in per_file.values())
    existing_growth = any(not s["new_file"] for s in per_file.values())
    if wrappers and existing_growth and total_add <= 40:
        _add("wrapper_growth", len(wrappers), [w["file"] for w in wrappers],
             [f"{w['file']}: {w['name']}" for w in wrappers],
             "小改动引入了新的顶层抽象；确认是否可复用已有实现、或写成内联逻辑。")

    # 6) The same field list declared in one place and enumerated in another.
    # Needs its own scan (declaration-vs-mapping key co-occurrence), so it is
    # appended rather than woven into the per-line loop above.
    out.extend(detect_field_list_duplication(pr_diff))

    return out


class AnalyzerSubAgent:
    """静态分析 SubAgent"""

    #: An added **top-level** public definition. The match is anchored at column 0
    #: on purpose: the previous pattern was applied to `line.lstrip("+").lstrip()`,
    #: which erased indentation, so every nested call and every added line that
    #: merely *started* with `identifier(` — `except (`, `isinstance(`, `super().__init__(`,
    #: `assert (`, wrapped test continuations — was reported as a public API change
    #: (20/20 false positives on a real self-review) and saturated the API term of
    #: `complexity_score`.
    PUBLIC_NAME_RE = re.compile(
        r"^(?:async\s+)?(?:def|class)\s+(?!_)([A-Za-z][A-Za-z0-9_]*)"
    )
    TODO_RE = re.compile(r"#\s*TODO|XXX|FIXME", re.IGNORECASE)
    EXCEP_RE = re.compile(r"\b(except|raise)\b")

    # 复杂度评分公式:
    #   score = diff_impact * DIFF_WEIGHT + api_impact * API_WEIGHT
    #   diff_impact = min(1.0, (additions + deletions) / DIFF_NORMALIZER)
    #   api_impact  = min(1.0, len(public_api_changes) / API_NORMALIZER)
    # DIFF_NORMALIZER: 超过 400 行变更即 diff_impact 达到 1.0
    # API_NORMALIZER:  超过 5 个 public API 变更即 api_impact 达到 1.0
    DIFF_NORMALIZER: float = 400.0
    API_NORMALIZER: float = 5.0
    DIFF_WEIGHT: float = 0.6
    API_WEIGHT: float = 0.4

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.name = "analyzer"
        self.capabilities = [
            "static_diff_analysis",
            "api_surface_check",
            "complexity_estimate",
        ]

    async def execute(self, context: Dict[str, Any]) -> AnalyzerResult:
        pr_diff = context.get("pr_diff", "") or ""
        old_content = context.get("old_content") or {}  # path -> str
        new_content = context.get("new_content") or {}
        hunks = self._split_hunks(pr_diff)
        additions = sum(h["additions"] for h in hunks)
        deletions = sum(h["deletions"] for h in hunks)
        files = list({h["file"] for h in hunks if h["file"]})

        public_api_changes: List[str] = []
        suspicious: List[Dict[str, str]] = []
        for h in hunks:
            # Test definitions are not part of the shipped public surface — see
            # _TEST_PATH_RE for why they must not feed complexity_score either.
            in_test_file = bool(_TEST_PATH_RE.search(h["file"] or ""))
            for line in h["lines"]:
                # NOTE: `line[1:]` (not .lstrip()) — indentation is load-bearing
                # for PUBLIC_NAME_RE, which only accepts top-level definitions.
                body = line[1:] if line.startswith("+") else line
                if (line.startswith("+") and not in_test_file
                        and self.PUBLIC_NAME_RE.match(body)):
                    public_api_changes.append(f"{h['file']}::{body.strip()[:120]}")
                if line.startswith("+") and self.TODO_RE.search(line):
                    suspicious.append({"file": h["file"], "hunk": h["header"], "kind": "todo_in_diff"})
                if line.startswith("+") and self.EXCEP_RE.search(line):
                    suspicious.append({"file": h["file"], "hunk": h["header"], "kind": "new_exception_path"})

        score = min(1.0, (additions + deletions) / self.DIFF_NORMALIZER) * self.DIFF_WEIGHT \
              + min(1.0, len(public_api_changes) / self.API_NORMALIZER) * self.API_WEIGHT

        return AnalyzerResult(
            total_hunks=len(hunks),
            total_additions=additions,
            total_deletions=deletions,
            files_changed=files,
            public_api_changes=public_api_changes[:20],
            suspicious_spots=suspicious[:20],
            complexity_score=round(score, 3),
            repeated_added_blocks=detect_repeated_added_blocks(pr_diff),
            bloat_signals=detect_bloat_signals(pr_diff),
        )

    def _split_hunks(self, pr_diff: str) -> List[Dict[str, Any]]:
        hunks: List[Dict[str, Any]] = []
        current_file = ""
        current_header = ""
        current_lines: List[str] = []
        cur_add, cur_del = 0, 0

        def flush():
            # NOTE: do NOT reset current_file here — a file spans multiple hunks,
            # and a @@-triggered flush must keep the file context for following lines.
            nonlocal current_header, current_lines, cur_add, cur_del
            if current_header:
                hunks.append({
                    "file": current_file,
                    "header": current_header,
                    "lines": current_lines,
                    "additions": cur_add,
                    "deletions": cur_del,
                })
            current_header = ""
            current_lines = []
            cur_add, cur_del = 0, 0

        for line in pr_diff.split("\n"):
            if line.startswith("diff --git"):
                flush()
                parts = line.split()
                current_file = parts[2].removeprefix("a/").removeprefix("b/") if len(parts) >= 3 else ""
            elif line.startswith("@@"):
                flush()
                current_header = line
            elif current_file and current_header:
                if line.startswith("+++") or line.startswith("---"):
                    continue
                current_lines.append(line)
                if line.startswith("+"):
                    cur_add += 1
                elif line.startswith("-"):
                    cur_del += 1
        flush()
        return hunks

    def get_capabilities(self) -> List[str]:
        return self.capabilities

    def get_status(self) -> Dict[str, Any]:
        return {"name": self.name, "type": "analyzer", "capabilities": self.capabilities}
