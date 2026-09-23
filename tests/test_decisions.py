"""decisions(laya typed-decision 层)的可选性契约 + 配置解析 + loop 接线。

核心契约:后端不可用 / 配置缺失 / 推理抛错时一切返回 None,loop
维持原有「总是 revise」行为 —— 无决策模型的环境零行为变化。
"""
import asyncio
import io
import json

import swe_review.tools.decisions as dec
from swe_review.subagents.loop_agent import LoopSubAgent

CANDIDATE = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


class _FakeLocalAgent:
    def __init__(self, action="regenerate", raises=False):
        self._action = action
        self._raises = raises
        self.seen_state = None

    def predict(self, state, spec):
        if self._raises:
            raise RuntimeError("boom")
        self.seen_state = state
        assert len(state) <= 3000  # 1024-token 上下文预算的字符级护栏
        return {"answers": {"action": {"type": "choice", "choice": self._action}}}


def _force_backend(monkeypatch, backend, cfg=None, agent=None):
    monkeypatch.setattr(dec, "_backend", backend)
    monkeypatch.setattr(dec, "_cfg", cfg or {})
    monkeypatch.setattr(dec, "_agent", agent)
    monkeypatch.setattr(dec, "_load_attempted", True)


def _write_cfg(monkeypatch, tmp_path, cfg):
    p = tmp_path / "decisions.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("SWE_DECISIONS_CONFIG", str(p))
    monkeypatch.setattr(dec, "_load_attempted", False)
    monkeypatch.setattr(dec, "_backend", None)


REV = {"decision": "request_changes", "confidence": 0.8,
       "defects": [{"severity": "P1", "description": "wrong layer"}],
       "findings": []}


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def test_backend_off_disables(monkeypatch, tmp_path):
    _write_cfg(monkeypatch, tmp_path, {"backend": "off"})
    assert dec.backend_name() is None
    assert dec.choose_revision_action(REV) is None


def test_backend_cloud_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    _write_cfg(monkeypatch, tmp_path, {"backend": "cloud"})
    assert dec.backend_name() is None  # 无 key → 不可用,不隐式降级


def test_missing_config_file_falls_back_to_auto(monkeypatch, tmp_path):
    monkeypatch.setenv("SWE_DECISIONS_CONFIG", str(tmp_path / "nope.json"))
    monkeypatch.setattr(dec, "_load_attempted", False)
    monkeypatch.setattr(dec, "_backend", None)
    monkeypatch.setattr(dec, "_local_importable", lambda: False)
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    assert dec.backend_name() is None  # 本地不可用 + 无 key → 禁用


def test_auto_prefers_local(monkeypatch, tmp_path):
    _write_cfg(monkeypatch, tmp_path, {"backend": "auto"})
    monkeypatch.setattr(dec, "_load_attempted", False)
    monkeypatch.setattr(dec, "_backend", None)
    monkeypatch.setattr(dec, "_local_importable", lambda: True)
    assert dec.backend_name() == "local"


# ---------------------------------------------------------------------------
# 推理行为(两个后端共用 extraction 逻辑)
# ---------------------------------------------------------------------------

def test_local_choice_and_validation(monkeypatch):
    _force_backend(monkeypatch, "local", agent=_FakeLocalAgent("regenerate"))
    assert dec.choose_revision_action(REV) == "regenerate"
    _force_backend(monkeypatch, "local", agent=_FakeLocalAgent("nonsense"))
    assert dec.choose_revision_action(REV) is None


def test_inference_error_returns_none(monkeypatch):
    _force_backend(monkeypatch, "local", agent=_FakeLocalAgent(raises=True))
    assert dec.choose_revision_action(REV) is None


def test_cloud_predict_shape(monkeypatch):
    """云端 /predict 响应与本地 answers 同构,走同一套提取逻辑。"""
    payload = {"answers": {"action": {"type": "choice", "choice": "revise",
                                      "probabilities": {"revise": 0.9}}}}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data)
        return _Resp(json.dumps(payload).encode())

    monkeypatch.setenv("LLM_GATEWAY_KEY", "sk-test")
    monkeypatch.setattr(dec.urllib.request, "urlopen", fake_urlopen)
    _force_backend(monkeypatch, "cloud",
                   cfg={"cloud": {"base_url": "http://x/laya"}})
    assert dec.choose_revision_action(REV) == "revise"
    assert seen["url"] == "http://x/laya/predict"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["questions"]["action"]["type"] == "choice"


def test_cloud_http_error_returns_none(monkeypatch):
    def boom(req, timeout): raise OSError("conn refused")
    monkeypatch.setattr(dec.urllib.request, "urlopen", boom)
    _force_backend(monkeypatch, "cloud",
                   cfg={"cloud": {"base_url": "http://x/laya"}})
    assert dec.choose_revision_action(REV) is None


def test_low_severity_never_regenerates(monkeypatch):
    """只有 P3 级琐事时直接返回 None(走 revise),不消费模型调用。"""
    agent = _FakeLocalAgent("regenerate")
    _force_backend(monkeypatch, "local", agent=agent)
    low = {"decision": "request_changes",
           "defects": [{"severity": "P3", "description": "naming"}],
           "findings": []}
    assert dec.choose_revision_action(low) is None
    assert agent.seen_state is None


# ---------------------------------------------------------------------------
# loop 接线
# ---------------------------------------------------------------------------

class _RejectingReview:
    async def execute(self, **kw):
        return dict(REV, token_usage=None)


class _RecordingRevise:
    def __init__(self):
        self.calls = 0

    async def execute(self, **kw):
        self.calls += 1
        return {"status": "success", "diff": CANDIDATE, "title": "t",
                "body": "", "changes_summary": "ok"}


class _RecordingGen:
    def __init__(self):
        self.calls = 0

    async def execute(self, **kw):
        self.calls += 1
        return {"title": "t", "body": "", "diff": CANDIDATE,
                "rationale": "fresh approach", "confidence": 0.7}


def _run(loop):
    return asyncio.run(loop.execute({
        "issue": "i", "repo_path": ".", "initial_pr": {"diff": CANDIDATE},
        "max_iterations": 2}))


def test_loop_regenerates_when_decision_layer_says_so(monkeypatch):
    _force_backend(monkeypatch, "local", agent=_FakeLocalAgent("regenerate"))
    gen, revise = _RecordingGen(), _RecordingRevise()
    loop = LoopSubAgent(review_skill=_RejectingReview(), revise_skill=revise,
                        generator_skill=gen, max_iterations=2)
    result = _run(loop)
    assert gen.calls == 1
    assert revise.calls == 0
    assert any(it.phase == "regenerate" and it.decision == "ok"
               for it in result.iterations)


def test_loop_falls_back_to_revise_without_backend(monkeypatch):
    _force_backend(monkeypatch, None)
    gen, revise = _RecordingGen(), _RecordingRevise()
    loop = LoopSubAgent(review_skill=_RejectingReview(), revise_skill=revise,
                        generator_skill=gen, max_iterations=2)
    result = _run(loop)
    assert revise.calls == 1
    assert gen.calls == 0
    assert not any(it.phase == "regenerate" for it in result.iterations)
