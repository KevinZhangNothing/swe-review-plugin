"""Deterministic grounding of review-finding locations (no LLM calls)."""
from swe_review.subagents.location_grounding import (
    extract_location_path,
    ground_report_locations,
)
from swe_review.subagents.reviewer_agent import Defect, Finding, ReviewReport


def _report(location):
    return ReviewReport(
        decision="request_changes", confidence=0.8,
        defects=[Defect(severity="high", description="bug",
                        location=location, suggestion="fix")],
        findings=[Finding(severity="P1", title="t", location=location)],
    )


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod_a.py").write_text("x = 1\ny = 2\n")
    (tmp_path / "pkg" / "mod_b.py").write_text("z = 3\n")
    return tmp_path


def test_exact_path_and_line_in_range_unchanged(tmp_path):
    repo = _repo(tmp_path)
    report = _report({"path": "pkg/mod_a.py", "start_line": 1, "end_line": 1})
    ground_report_locations(report, repo_path=str(repo))
    assert report.defects[0].location["path"] == "pkg/mod_a.py"
    assert report.defects[0].location["relocated_from"] is None
    assert report.findings[0].location["verified"] is True


def test_missing_path_relocates_by_unique_basename(tmp_path):
    repo = _repo(tmp_path)
    report = _report({"path": "src/wrong/dir/mod_a.py", "start_line": 2, "end_line": 2})
    ground_report_locations(report, repo_path=str(repo))
    loc = report.defects[0].location
    assert loc["path"] == "pkg/mod_a.py"
    assert loc["relocated_from"] == "src/wrong/dir/mod_a.py"
    assert report.findings[0].location["path"] == "pkg/mod_a.py"


def test_out_of_range_line_flagged_but_path_kept(tmp_path):
    repo = _repo(tmp_path)
    report = _report({"path": "pkg/mod_a.py", "start_line": 99, "end_line": 120})
    ground_report_locations(report, repo_path=str(repo))
    loc = report.defects[0].location
    assert loc["path"] == "pkg/mod_a.py"
    assert loc["line_verified"] is False
    assert loc["file_lines"] == 2


def test_ambiguous_basename_marked_unverified(tmp_path):
    repo = _repo(tmp_path)
    (repo / "dup.py").write_text("a\n")
    (repo / "pkg" / "dup.py").write_text("b\n")
    report = _report({"path": "nowhere/dup.py", "start_line": 1, "end_line": 1})
    ground_report_locations(report, repo_path=str(repo))
    loc = report.defects[0].location
    assert loc["verified"] is False
    assert loc["relocated_from"] is None


def test_flat_string_locations_supported(tmp_path):
    repo = _repo(tmp_path)
    report = ReviewReport(
        decision="request_changes", confidence=0.8,
        defects=[Defect(severity="high", description="d",
                        location="pkg/mod_b.py:1", suggestion="s")],
    )
    ground_report_locations(report, repo_path=str(repo))
    assert report.defects[0].location == "pkg/mod_b.py:1 [verified]"


def test_flat_annotation_is_idempotent(tmp_path):
    """Grounding the same flat location twice must not nest suffixes.

    Documents the contract in `ground_report_locations`: the annotated string
    no longer ends in a bare `:digits` line field, so a second pass leaves it
    alone instead of appending another `[verified]`.
    """
    repo = _repo(tmp_path)
    report = ReviewReport(
        decision="request_changes", confidence=0.8,
        defects=[Defect(severity="high", description="d",
                        location="pkg/mod_b.py:1", suggestion="s")],
    )
    ground_report_locations(report, repo_path=str(repo))
    once = report.defects[0].location
    ground_report_locations(report, repo_path=str(repo))
    assert report.defects[0].location == once == "pkg/mod_b.py:1 [verified]"


def test_no_repo_path_is_noop():
    report = _report({"path": "any.py", "start_line": 1, "end_line": 1})
    ground_report_locations(report, repo_path=None)
    assert report.defects[0].location.get("verified") is None


def test_grounding_runs_in_review_skill(tmp_path, monkeypatch):
    """ReviewSkill.execute applies grounding after parsing."""
    import asyncio
    from swe_review import ReviewSkill

    repo = _repo(tmp_path)

    class FakeAdapter:
        async def chat(self, **kw):
            import json
            payload = {
                "decision": {"recommendation": "request_changes",
                             "confidence": 0.8},
                "defects": [{
                    "severity": "high", "category": "correctness",
                    "description": "bug",
                    "location": {"path": "stale/dir/mod_b.py",
                                 "start_line": 1, "end_line": 1},
                    "suggestion": "fix",
                }],
            }
            return json.dumps(payload), {"prompt_tokens": 1,
                                         "completion_tokens": 1,
                                         "total_tokens": 2}

    skill = ReviewSkill(tool_adapter=FakeAdapter(), prompt_style="detailed")
    res = asyncio.run(skill.execute(issue="x", pr_title="t", pr_diff="diff",
                                    repo_path=str(repo), deep=True))
    loc = res.payload["defects"][0]["location"]
    assert loc["path"] == "pkg/mod_b.py"
    assert loc["relocated_from"] == "stale/dir/mod_b.py"


def test_path_traversal_never_escapes_repo(tmp_path):
    """Absolute paths and .. traversal must not read files outside the repo."""
    repo = _repo(tmp_path)
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("secret = 1\n" * 40)
    for loc in ({"path": str(outside), "start_line": 1, "end_line": 1},
                {"path": "../outside_secret.py", "start_line": 1, "end_line": 1}):
        report = _report(loc)
        ground_report_locations(report, repo_path=str(repo))
        d = report.defects[0].location
        assert d["verified"] is False
        assert d["relocated_from"] is None
        # must not leak the outside file's line count
        assert d.get("file_lines") is None
    # original path is kept verbatim (annotation, not destruction), but no
    # content-derived info from outside the repo may appear
    assert report.defects[0].location.get("file_lines") is None


def test_repo_scanned_once_per_report(tmp_path, monkeypatch):
    """One stale location triggers exactly one full scan, not one per defect."""
    repo = _repo(tmp_path)
    import swe_review.subagents.location_grounding as mod
    calls = []
    real = mod._repo_files

    def counting(r):
        calls.append(1)
        return real(r)

    monkeypatch.setattr(mod, "_repo_files", counting)
    report = ReviewReport(
        decision="request_changes", confidence=0.8,
        defects=[Defect(severity="high", description=f"d{i}",
                        location={"path": f"stale{i}/mod_a.py", "start_line": 1,
                                  "end_line": 1}, suggestion="s")
                 for i in range(5)],
    )
    ground_report_locations(report, repo_path=str(repo))
    assert len(calls) == 1
    assert all(d.location["path"] == "pkg/mod_a.py" for d in report.defects)


def test_extract_location_path_variants():
    assert extract_location_path("a/b.py:12") == "a/b.py"
    assert extract_location_path({"path": "a/b.py"}) == "a/b.py"
    assert extract_location_path("") == ""
    assert extract_location_path({"start_line": 1}) == ""
