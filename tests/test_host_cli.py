"""CLI: --tool host + `swe-review host` subcommand.

The contract any agent follows: run -> exit 3 (awaiting_host) with a JSON
envelope -> answer the pending prompt -> re-run the same command -> exit 0.
"""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from swe_review.cli import EXIT_AWAITING_HOST, _cmd_list_tools, build_parser, main
from swe_review.tools import ADAPTER_NAMES, build_adapter

SAMPLE_DIFF = """diff --git a/a.py b/a.py
new file mode 100644
--- /dev/null
+++ b/a.py
@@ -0,0 +1,1 @@
+value = 2
"""

GOOD_REVIEW = json.dumps({
    "decision": "approve", "confidence": 0.9,
    "summary": {"problem": "p", "solution": "s", "overall_assessment": "ok"},
    "defects": [],
})


def _write_diff(tmp_path):
    (tmp_path / "p.diff").write_text(SAMPLE_DIFF)
    return str(tmp_path / "p.diff")


@pytest.fixture()
def host_dir(tmp_path):
    return tmp_path / "host"


def _review_cmd(tmp_path, diff, host_dir):
    return ["review", "--issue", "i", "--pr-title", "t", "--pr-diff", diff,
            "--repo-path", str(tmp_path), "--max-steps", "2",
            "--prompt-style", "concise", "--tool", "host",
            "--host-dir", str(host_dir), "--agent", "test-agent"]


def test_registry_knows_host():
    assert "host" in ADAPTER_NAMES
    a = build_adapter("host", host_dir="x", agent="a")
    assert a.name == "host" and a.work_dir.name == "x"


def test_tool_choices_come_from_registry():
    """--tool must never drift from the registry (3 sites used to hand-code it)."""
    import argparse

    parser = build_parser()
    seen = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                for a in sub._actions:
                    if "--tool" in a.option_strings:
                        seen.append(a.choices)
    assert len(seen) == 3, f"expected review/revise/loop --tool, saw {len(seen)}"
    for choices in seen:
        assert list(choices) == list(ADAPTER_NAMES)


def test_parser_accepts_host_dir_and_agent():
    args = build_parser().parse_args(
        ["loop", "--issue", "x", "--tool", "host",
         "--host-dir", "/tmp/hd", "--agent", "cline"])
    assert args.tool == "host" and args.host_dir == "/tmp/hd" and args.agent == "cline"


def test_list_tools_includes_host(capsys):
    asyncio.run(_cmd_list_tools(SimpleNamespace()))
    names = [t["name"] for t in json.loads(capsys.readouterr().out)["tools"]]
    assert "host" in names


def test_review_host_roundtrip(tmp_path, host_dir, capsys):
    """The full agent contract: run -> 3 -> answer -> same run -> 0."""
    diff = _write_diff(tmp_path)
    cmd = _review_cmd(tmp_path, diff, host_dir)

    # 1) run: no answer yet -> exit 3 + machine-readable envelope
    code = main(cmd)
    assert code == EXIT_AWAITING_HOST
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "awaiting_host"
    assert envelope["pending_count"] == 1
    assert envelope["request_path"].startswith(str(host_dir))
    assert "spawn" in envelope["instruction"]

    # 2) host pending lists the prompt (brief by default: no prompt text)
    assert main(["host", "pending", "--host-dir", str(host_dir),
                 "--agent", "test-agent"]) == 0
    pend = json.loads(capsys.readouterr().out)
    assert pend["pending_count"] == 1
    assert pend["requests"][0]["key"] == envelope["key"]
    assert "system" not in pend["requests"][0]

    # 3) host status
    assert main(["host", "status", "--host-dir", str(host_dir),
                 "--agent", "test-agent"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert (st["requests"], st["answered"], st["pending"]) == (1, 0, 1)

    # 4) answer (bare model output accepted directly, like the review JSON)
    assert main(["host", "answer", "--host-dir", str(host_dir),
                 "--agent", "test-agent", "--key", envelope["key"],
                 "--text", GOOD_REVIEW]) == 0
    capsys.readouterr()  # drop the answer confirmation before asserting on the review

    # 5) re-run the exact same command -> replayed from cache -> exit 0
    assert main(cmd) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "approve"
    assert payload["token_usage"]["total_tokens"] > 0


def test_host_answer_reads_stdin_dash(tmp_path, host_dir, monkeypatch, capsys):
    """--text '-' must read stdin (it previously recorded the literal '-')."""
    diff = _write_diff(tmp_path)
    code = main(["review", "--issue", "i", "--pr-diff", diff,
                 "--repo-path", str(tmp_path), "--max-steps", "2",
                 "--prompt-style", "concise", "--tool", "host",
                 "--host-dir", str(host_dir)])
    assert code == EXIT_AWAITING_HOST
    key = json.loads(capsys.readouterr().out)["key"]

    monkeypatch.setattr("sys.stdin", io.StringIO("stdin answer"))
    assert main(["host", "answer", "--host-dir", str(host_dir),
                 "--key", key, "--text", "-"]) == 0

    resp = host_dir / "responses" / f"{key}.json"
    assert resp.is_file()
    assert json.loads(resp.read_text())["text"] == "stdin answer"


def test_host_answer_rejects_unknown_key_and_empty(tmp_path, host_dir, capsys):
    # Well-formed but unknown key -> KeyError (format check happens first)
    with pytest.raises(KeyError):
        main(["host", "answer", "--host-dir", str(host_dir),
              "--key", "0" * 16, "--text", "x"])
    diff = _write_diff(tmp_path)
    code = main(["review", "--issue", "i", "--pr-diff", diff,
                 "--repo-path", str(tmp_path), "--max-steps", "2",
                 "--prompt-style", "concise", "--tool", "host",
                 "--host-dir", str(host_dir)])
    assert code == EXIT_AWAITING_HOST
    key = json.loads(capsys.readouterr().out)["key"]
    with pytest.raises(ValueError, match="refusing to record an empty answer"):
        main(["host", "answer", "--host-dir", str(host_dir),
              "--key", key, "--text", "   "])


def test_host_pending_show_includes_prompt(tmp_path, host_dir, capsys):
    diff = _write_diff(tmp_path)
    code = main(["review", "--issue", "i", "--pr-diff", diff,
                 "--repo-path", str(tmp_path), "--max-steps", "2",
                 "--prompt-style", "concise", "--tool", "host",
                 "--host-dir", str(host_dir)])
    assert code == EXIT_AWAITING_HOST
    capsys.readouterr()
    assert main(["host", "pending", "--host-dir", str(host_dir),
                 "--show"]) == 0
    req = json.loads(capsys.readouterr().out)["requests"][0]
    assert "system" in req and "user" in req
    assert req["prompt_chars"] > 0


def test_host_answer_rejects_traversal_key(tmp_path, host_dir, capsys):
    """P2 regression at the CLI layer: a traversal key must not write outside
    the rendezvous dir."""
    diff = _write_diff(tmp_path)
    assert main(["review", "--issue", "i", "--pr-diff", diff,
                 "--repo-path", str(tmp_path), "--max-steps", "2",
                 "--prompt-style", "concise", "--tool", "host",
                 "--host-dir", str(host_dir)]) == EXIT_AWAITING_HOST
    capsys.readouterr()
    outside = tmp_path / "outside.json"
    assert not outside.exists()
    with pytest.raises(ValueError, match="invalid prompt key"):
        main(["host", "answer", "--host-dir", str(host_dir),
              "--key", "../outside", "--text", "x"])
    assert not outside.exists()

