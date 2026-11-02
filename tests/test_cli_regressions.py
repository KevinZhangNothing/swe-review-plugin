"""CLI-surface regressions from the v1.0.0 architecture review.

These three defects all shared one property: the command exited 0 and printed
something that looked like success, so nobody noticed the work never happened.
"""

import pytest

from swe_review.cli import build_parser
from swe_review.tools import ADAPTER_NAMES


# ---------------------------------------------------------------------------
# P0-3: `loop` must accept the sandbox toggles its own help refers to
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["verify", "loop"])
def test_sandbox_flags_exist_on_verify_and_loop(cmd):
    """`loop` lacked --sandbox/--no-sandbox while --compile-cmd's help said to
    "pair with --no-sandbox for incremental builds" — an unreachable instruction,
    and `loop` was permanently sandboxed via getattr(args, "sandbox", True)."""
    args = build_parser().parse_args([cmd, "--issue", "i", "--tool", "shell"]
                                     if cmd == "loop" else
                                     [cmd, "--pr-diff", "-"])
    assert args.sandbox is True


def test_loop_no_sandbox_is_accepted():
    args = build_parser().parse_args(
        ["loop", "--issue", "i", "--tool", "shell", "--no-sandbox"])
    assert args.sandbox is False


def test_loop_build_config_receives_sandbox_choice():
    """The flag must actually reach the verifier config, not just parse."""
    from swe_review.cli import _build_check_config
    args = build_parser().parse_args(
        ["loop", "--issue", "i", "--tool", "shell", "--no-sandbox"])
    assert _build_check_config(args)["sandbox"] is False


# ---------------------------------------------------------------------------
# P0-4: `--tool` must never resolve to the offline stub
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["review", "revise", "loop"])
def test_tool_defaults_to_auto_not_shell(cmd):
    """Defaulting to `shell` printed a scored, confident report that had called
    no LLM at all — the worst possible failure mode for an AI self-review tool.

    The fix is `auto` (first CLI on PATH), NOT `required`: an omitted flag must
    keep working for users who have exactly one CLI installed.
    """
    argv = [cmd, "--issue", "i"]
    if cmd == "review":
        argv += ["--pr-diff", "-"]
    if cmd == "revise":
        argv += ["--pr-diff", "-", "--review-report", "-"]
    args = build_parser().parse_args(argv)
    assert args.tool == "auto"


def test_shell_is_still_selectable_but_never_automatic():
    args = build_parser().parse_args(
        ["review", "--issue", "i", "--pr-diff", "-", "--tool", "shell"])
    assert args.tool == "shell"
    assert "shell" in ADAPTER_NAMES
    from swe_review.tools import AUTO_DETECT_ORDER
    assert "shell" not in AUTO_DETECT_ORDER
    assert "host" not in AUTO_DETECT_ORDER


def test_auto_resolves_to_an_installed_cli(monkeypatch):
    """Detection picks the first adapter whose OWN resolved cli_path runs.

    Detection asks the adapter what it would exec, then checks that path — it
    does not re-derive a binary name, because those two disagreed for opencode
    (default install dir vs $PATH).
    """
    import swe_review.tools as tools

    monkeypatch.setattr(tools, "detect_adapter", lambda: "pi")
    assert tools.resolve_adapter_name("auto") == "pi"
    assert tools.resolve_adapter_name(None) == "pi"


def test_detection_uses_the_adapters_resolved_path(monkeypatch):
    """Only cli_path counts — a bare binary name on PATH is not enough."""
    import swe_review.tools as tools

    class _Fake:
        def __init__(self, cli):
            self.cli_path = cli

    resolved = {"claude-code": "/opt/claude", "cursor": "/opt/agent",
                "opencode": "opencode", "pi": "/opt/pi"}
    monkeypatch.setattr(tools, "build_adapter", lambda n: _Fake(resolved[n]))
    # `opencode` resolves to a bare name here, so `which` gets called with it.
    monkeypatch.setattr(tools.shutil, "which",
                        lambda p: p if p == "/opt/pi" else None)
    assert tools.detect_adapter() == "pi"


def test_auto_prefers_registry_order(monkeypatch):
    """When several CLIs exist, preference order is deterministic."""
    import swe_review.tools as tools

    monkeypatch.setattr(tools.shutil, "which", lambda p: p)
    assert tools.detect_adapter() == tools.AUTO_DETECT_ORDER[0]


def test_auto_with_nothing_installed_fails_loudly(monkeypatch):
    """No silent fallback to the stub: dedicated exception naming what was searched."""
    import swe_review.tools as tools

    monkeypatch.setattr(tools.shutil, "which", lambda b: None)
    assert tools.detect_adapter() is None
    with pytest.raises(tools.ToolNotFound) as exc:
        tools.resolve_adapter_name("auto")
    assert "no LLM CLI found" in str(exc.value)
    assert set(exc.value.looked_for) == {"claude", "agent", "opencode", "pi"}


def test_tool_not_found_is_not_a_value_error():
    """The CLI distinguishes this by TYPE, so it must not be a bare ValueError —
    otherwise `main` would have to match on message text to pick an exit path."""
    import swe_review.tools as tools

    assert issubclass(tools.ToolNotFound, LookupError)
    assert not issubclass(tools.ToolNotFound, ValueError)


def test_explicit_tool_bypasses_detection(monkeypatch):
    """An explicit --tool must work even when nothing is on PATH."""
    import swe_review.tools as tools

    monkeypatch.setattr(tools.shutil, "which", lambda b: None)
    assert tools.resolve_adapter_name("host") == "host"
    assert tools.resolve_adapter_name("shell") == "shell"


def test_auto_failure_exits_2_without_traceback(monkeypatch, capsys):
    from swe_review.cli import main
    import swe_review.tools as tools

    monkeypatch.setattr(tools.shutil, "which", lambda b: None)
    code = main(["review", "--issue", "i", "--pr-diff", "-", "--tool", "auto"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no LLM CLI found" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# P0-5: install-skills must not silently install nothing
# ---------------------------------------------------------------------------

def test_install_skills_default_source_resolves_from_the_package():
    """The default was `skills_md`, a directory that does not exist, so
    `install-skills` reported `{"installed": {}}` and exited 0.

    The replacement must resolve independently of the process CWD — asserting on
    a relative default made this test pass only when run from the repo root, and
    the same bug made the COMMAND fail when run from anywhere else.
    """
    import os
    from pathlib import Path

    from swe_review.tools.pi_adapter import PiAdapter

    args = build_parser().parse_args(["install-skills"])
    # No literal default: PiAdapter owns resolution (packaged dir, then repo).
    assert args.source is None

    adapter = PiAdapter(skills_dir="/tmp/unused", skills_source_dir=None)
    source = Path(adapter.skills_source_dir)
    assert source.is_absolute(), (
        f"skill source must not depend on CWD, got {source}")
    assert source.is_dir(), f"resolved --source {source} does not exist"
    assert any((p / "SKILL.md").exists() for p in source.iterdir() if p.is_dir())

    # The real proof: resolution is unaffected by where we are standing.
    cwd = os.getcwd()
    try:
        os.chdir("/")
        again = Path(PiAdapter(skills_dir="/tmp/unused",
                               skills_source_dir=None).skills_source_dir)
    finally:
        os.chdir(cwd)
    assert again == source


def test_install_skills_fails_loudly_on_empty_source(tmp_path, capsys):
    from swe_review.cli import main
    empty = tmp_path / "empty"
    empty.mkdir()
    # Returned code, not SystemExit: every handler exits the same way.
    code = main(["install-skills", "--source", str(empty),
                 "--pi-skills-dir", str(tmp_path / "dest")])
    assert code != 0
    captured = capsys.readouterr()
    assert "nothing installed" in (captured.err + captured.out)


# ---------------------------------------------------------------------------
# Degradations must be machine-readable, not prose
# ---------------------------------------------------------------------------

def test_shell_review_is_reported_as_degraded(capsys, tmp_path):
    """A `shell` review is structurally complete but evidence-free; callers need
    a field telling them so instead of parsing summary text."""
    import json
    from swe_review.cli import main

    diff = tmp_path / "pr.diff"
    diff.write_text(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1 @@\n-a\n+b\n")
    code = main(["review", "--issue", "i", "--pr-diff", str(diff),
                 "--tool", "shell", "--prompt-style", "concise",
                 "--repo-path", str(tmp_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "degraded" in payload
    assert any("shell_placeholder_backend" in d for d in payload["degraded"])
    assert payload["tool_name"] == "shell"


def test_real_adapter_review_is_not_marked_degraded(tmp_path):
    """Negative control: the degraded tag must not fire for a genuine review.

    Uses a stub adapter that returns a real report — not `--tool host`, whose
    awaiting_host envelope proves nothing about the review path.
    """
    import asyncio
    import json

    from swe_review import ReviewSkill
    from conftest import StubAdapter

    skill = ReviewSkill(tool_adapter=StubAdapter("working-stub"),
                        prompt_style="concise")
    result = asyncio.run(skill.execute(
        issue="i", pr_title="t",
        pr_diff="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                "@@ -1 +1 @@\n-a\n+b\n",
        prompt_style="concise"))

    assert result.payload["tool_name"] == "working-stub"
    assert "degraded" not in result.payload


def test_shell_revision_is_also_reported_as_degraded():
    """`--tool shell` reaches revise too; tagging only review left the same
    zero-evidence failure mode unlabelled in the sibling command."""
    import asyncio

    from swe_review import ReviseSkill
    from swe_review.tools import build_adapter

    skill = ReviseSkill(tool_adapter=build_adapter("shell"), prompt_style="concise")
    result = asyncio.run(skill.execute(
        issue="i", original_pr_title="t",
        original_pr_diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        review_report={"decision": "request_changes", "defects": []}))
    assert any("shell_placeholder_backend" in d
               for d in result.payload.get("degraded", []))

# ---------------------------------------------------------------------------
# `auto` failure must behave identically on every command path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["review", "--issue", "i", "--pr-diff", "-"],
    ["revise", "--issue", "i", "--pr-diff", "-", "--review-report", "-"],
    ["loop", "--issue", "i"],
], ids=["review", "revise", "loop"])
def test_auto_failure_is_identical_across_commands(monkeypatch, capsys, argv):
    """ToolNotFound is caught at one place (`main`'s outer try), so every
    handler must reach it rather than leaking a traceback.

    Pinned per-command because a future `except` inside one handler would
    silently give that command a different exit path from its siblings.
    """
    import swe_review.tools as tools
    from swe_review.cli import main
    from swe_review.tools import ToolNotFound

    monkeypatch.setattr(tools, "detect_adapter", lambda: None)
    monkeypatch.setattr(tools.shutil, "which", lambda b: None)

    code = main([*argv, "--tool", "auto"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no LLM CLI found" in err
    assert "Traceback" not in err


def test_auto_failure_message_reflects_real_probe_targets(monkeypatch):
    """`looked_for` must come from the same resolution detection used.

    A hardcoded literal would drift from the adapters' actual cli_path — the
    exact class of bug that made `shutil.which("opencode")` dead code.
    """
    import swe_review.tools as tools

    monkeypatch.setattr(tools, "_resolved_cli_paths",
                        lambda: {"pi": "/custom/pi-bin"})
    monkeypatch.setattr(tools.shutil, "which", lambda b: None)
    with pytest.raises(tools.ToolNotFound) as exc:
        tools.resolve_adapter_name("auto")
    assert exc.value.looked_for == ("/custom/pi-bin",)
    assert "/custom/pi-bin" in str(exc.value)


def test_revise_judges_degradation_from_result_not_constructor():
    """The degradation decision must read the adapter that produced the result.

    `ReviseSkill` used to judge from `self.tool.name` (fixed at construction)
    while `ReviewSkill` judged from `report.tool_name` (carried on the result).
    Both happened to agree, but two sources for one fact is how a rename silently
    stops tagging one command. `RevisedPR` now carries `tool_name` too.
    """
    import asyncio

    from swe_review import ReviseSkill
    from swe_review.tools import build_adapter

    shell = build_adapter("shell")
    skill = ReviseSkill(tool_adapter=shell, prompt_style="concise")
    result = asyncio.run(skill.execute(
        issue="i", original_pr_title="t",
        original_pr_diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        review_report={"decision": "request_changes", "defects": []}))

    assert result.payload["tool_name"] == "shell"
    assert any("shell_placeholder_backend" in d
               for d in result.payload.get("degraded", []))


def test_revise_result_carries_the_adapter_that_ran():
    """A skill constructed with adapter A must not report adapter B's name."""
    import asyncio

    from swe_review import ReviseSkill
    from conftest import StubAdapter

    class _Named(StubAdapter):
        name = "custom-reviser"

        def payload(self):
            return {"diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
                            "@@ -1 +1 @@\n-a\n+b\n",
                    "changes_summary": "s", "addressed_defect_indices": [0]}

    skill = ReviseSkill(tool_adapter=_Named(), prompt_style="concise")
    result = asyncio.run(skill.execute(
        issue="i", original_pr_title="t",
        original_pr_diff="diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
        review_report={"decision": "request_changes", "defects": []}))

    assert result.payload["tool_name"] == "custom-reviser"
    # A real backend is not degraded.
    assert "degraded" not in result.payload


def test_detection_constructs_each_adapter_once(monkeypatch):
    """The probe must not construct every adapter twice per resolve.

    Adapter construction can touch the filesystem (ClaudeCodeAdapter reads
    ~/.claude/settings.json), so running it twice for one question is a real
    side effect, not just wasted microseconds.
    """
    import swe_review.tools as tools

    calls: list = []
    real = tools.build_adapter

    def counted(name, **kwargs):
        calls.append(name)
        return real(name, **kwargs)

    monkeypatch.setattr(tools, "build_adapter", counted)
    tools._reset_detection_cache()

    try:
        tools.resolve_adapter_name("auto")
    except tools.ToolNotFound:
        pass

    assert len(calls) == len(tools.AUTO_DETECT_ORDER), (
        f"expected one construction per adapter, saw {calls}")


def test_detection_cache_is_resettable(monkeypatch):
    """A cached map must be droppable without restarting the process."""
    import swe_review.tools as tools

    monkeypatch.setattr(tools, "_resolved_cli_paths",
                        lambda: {"pi": "/first/pi"})
    assert tools._resolved_cli_paths() == {"pi": "/first/pi"}

    # Restore the real function and prove the reset clears memoized state.
    monkeypatch.undo()
    tools._reset_detection_cache()
    assert tools._CLI_PATHS_CACHE is None


def test_packaged_skill_sources_are_in_sync():
    """`install-skills` resolves sources from the INSTALLED package.

    A wheel only ships declared package data, so the editable `.claude/skills/`
    must be mirrored into `swe_review/.claude_skills/`. Without the mirror a
    `pip install` produced a distribution with no skill sources and the command
    installed 0 files while exiting 0 (now a hard error). This gate keeps the
    two copies from drifting.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "sync_skill_sources.py"
    assert script.is_file(), "sync script is missing"

    result = subprocess.run([sys.executable, str(script), "--check"],
                            capture_output=True, text=True, cwd=root)
    assert result.returncode == 0, (
        f"skill sources out of sync — run scripts/sync_skill_sources.py\n"
        f"{result.stdout}{result.stderr}")


def test_packaged_skill_sources_are_declared_as_package_data():
    """Mirroring into the package is useless if the wheel does not ship it."""
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    # Ignore comments: the file documents both rejected and accepted options.
    active = [line for line in pyproject.splitlines()
              if not line.lstrip().startswith("#")]

    assert any(".claude_skills/*/SKILL.md" in line for line in active), (
        "skill sources are not declared as package data")
    # `data-files` installs to sys.prefix, not into the importable package.
    assert not any(line.strip() == "[tool.setuptools.data-files]"
                   for line in active), (
        "data-files would install the skills outside the package")


def test_installed_package_carries_its_skill_sources():
    """End-to-end packaging gate: the skills must be importable package data.

    `install-skills` reads sources from the installed package directory. This
    asserts the mirror is present *next to the installed module*, which is what
    a wheel provides — a repo-checkout test would pass even if packaging dropped
    them, because the dev fallback (`.claude/skills/`) exists there.
    """
    from pathlib import Path

    import swe_review

    packaged = Path(swe_review.__file__).resolve().parent / ".claude_skills"
    assert packaged.is_dir(), (
        f"{packaged} is missing — skill sources are not shipped as package data")
    skills = sorted(p.parent.name for p in packaged.glob("*/SKILL.md"))
    assert len(skills) == 7, f"expected 7 packaged skills, found {skills}"


def test_empty_string_tool_is_treated_as_auto(monkeypatch):
    """Programmatic callers may pass ""; the contract says it means "detect".

    `--tool` has a default of "auto", so only code that builds a namespace by
    hand can produce "". Pinned because it is the difference between a helpful
    detection and an "unknown adapter" error for an empty value.
    """
    import swe_review.tools as tools

    monkeypatch.setattr(tools, "detect_adapter", lambda: "pi")
    assert tools.resolve_adapter_name("") == "pi"
    assert tools.resolve_adapter_name("auto") == "pi"
    assert tools.resolve_adapter_name(None) == "pi"


def test_build_adapter_rejects_auto_with_actionable_message():
    """`--tool auto` is a request to detect, not an adapter.

    It is a valid argparse choice, so a caller can reach `build_adapter("auto")`
    by skipping `resolve_adapter_name`. Saying "unknown adapter" would invite a
    retry with the same value; the message must name the missing step.
    """
    from swe_review.tools import build_adapter

    for value in ("auto", None):
        with pytest.raises(ValueError) as exc:
            build_adapter(value)
        assert "request to detect" in str(exc.value)
        assert "resolve_adapter_name" in str(exc.value)


def test_packaged_skills_start_with_yaml_frontmatter():
    """A SKILL.md must BEGIN with its `---` frontmatter block.

    The packaged copies are byte-identical mirrors. This pins that: a provenance
    banner (even an HTML comment) pushed `name:`/`description:` off line 1 and
    broke the loader that reads them — the installed skills became undiscoverable
    while every other test still passed.
    """
    from pathlib import Path

    import swe_review

    packaged = Path(swe_review.__file__).resolve().parent / ".claude_skills"
    skills = sorted(packaged.glob("*/SKILL.md"))
    assert skills, "no packaged skills to check"

    for path in skills:
        first_line = path.read_text(encoding="utf-8").split("\n", 1)[0]
        assert first_line == "---", (
            f"{path.name} does not start with YAML frontmatter "
            f"(first line: {first_line[:60]!r})")
        head = path.read_text(encoding="utf-8").split("\n")[:4]
        assert any(line.startswith("name:") for line in head), (
            f"{path.parent.name}/SKILL.md has no `name:` in its frontmatter")


def test_packaged_skills_are_byte_identical_to_sources():
    """No transformation at all — the mirror is a copy, not a render."""
    from pathlib import Path

    import swe_review

    root = Path(__file__).resolve().parent.parent
    packaged = Path(swe_review.__file__).resolve().parent / ".claude_skills"
    for mirror in sorted(packaged.glob("*/SKILL.md")):
        source = root / ".claude" / "skills" / mirror.parent.name / "SKILL.md"
        assert source.is_file(), f"no source for packaged {mirror.parent.name}"
        assert mirror.read_bytes() == source.read_bytes(), (
            f"{mirror.parent.name}/SKILL.md differs from its source")


def test_generate_is_also_reported_as_degraded():
    """The degradation contract must cover every skill that calls an adapter.

    `--tool shell` yields no usable diff from generate (its fixed payload has no
    `diff` key, so parsing returns ""). `ok=False` alone does not say WHY, and a
    caller comparing backends needs that reason — the same one review/revise
    report.
    """
    import asyncio

    from swe_review import GenerateSkill
    from swe_review.tools import build_adapter

    result = asyncio.run(
        GenerateSkill(tool_adapter=build_adapter("shell"))
        .execute(issue="i", repo_path=None, exploration={}))

    assert result.ok is False  # no diff was produced
    assert any("shell_placeholder_backend" in d
               for d in result.payload.get("degraded", []))
    # An explicit backend name, so the reason is not just "something was off".
    assert result.payload["degraded"]


def test_real_generate_is_not_marked_degraded():
    """Negative control for the generate path."""
    import asyncio

    from swe_review import GenerateSkill
    from conftest import SEED_DIFF, StubAdapter

    class _GenAdapter(StubAdapter):
        name = "gen-stub"

        def payload(self):
            return {"title": "t", "body": "", "diff": SEED_DIFF,
                    "rationale": "r", "confidence": 0.5}

    result = asyncio.run(
        GenerateSkill(tool_adapter=_GenAdapter())
        .execute(issue="i", repo_path=None, exploration={}))

    assert result.ok is True
    assert "degraded" not in result.payload


def test_generate_explore_marks_no_repo_as_degraded():
    """`explore()` returning `{}` hid the difference between "looked and found
    nothing" from "never looked"."""
    import asyncio

    from swe_review import GenerateSkill

    result = asyncio.run(GenerateSkill(tool_adapter=None).explore("i", None))
    assert "explore_skipped" in result["exploration_degraded"]


def test_generate_explore_marks_empty_result_as_degraded(tmp_path):
    """Keywords come from the issue text, so they are NOT evidence of a match."""
    import asyncio

    from swe_review import GenerateSkill

    (tmp_path / "unrelated.py").write_text("value = 1\n")
    result = asyncio.run(
        GenerateSkill(tool_adapter=None)
        .explore("zzz_no_such_symbol_anywhere", str(tmp_path)))
    assert result["keywords"], "precondition: keywords are always derived"
    assert "explore_empty" in result["exploration_degraded"]


def test_generate_explore_with_evidence_is_not_degraded(tmp_path):
    """Negative control: resolving a real file must clear the degraded tag."""
    import asyncio

    from swe_review import GenerateSkill

    (tmp_path / "target.py").write_text("def unique_marker_fn():\n    return 42\n")
    result = asyncio.run(
        GenerateSkill(tool_adapter=None)
        .explore("unique_marker_fn returns wrong value", str(tmp_path)))
    assert result["related_files"] or result["files_modified"]
    assert "exploration_degraded" not in result
