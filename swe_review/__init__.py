"""SWE-Review - Agentic Code Review (close the loop on issue resolution)."""

from .skill import ReviewSkill, ReviseSkill, ExploreSkill, VerifySkill, LoopSkill, AnalyzeSkill, GenerateSkill
from .subagents.reviewer_agent import (
    ReviewerSubAgent, ReviewReport, Defect, Finding,
    DECISION_CHOICES, DECISION_APPROVING, DECISION_REJECTING,
)
from .subagents.reviser_agent import ReviserSubAgent, RevisedPR
from .subagents.explorer_agent import ExplorerSubAgent, ExplorationResult
from .subagents.verifier_agent import VerifierSubAgent, VerificationResult
from .subagents.analyzer_agent import AnalyzerSubAgent, AnalyzerResult
from .subagents.generator_agent import GeneratorSubAgent, GeneratedPR
from .subagents.loop_agent import LoopSubAgent, LoopResult, LoopIteration
from .tools.claude_code_adapter import ClaudeCodeAdapter
from .tools.cursor_adapter import CursorAdapter
from .tools.opencode_adapter import OpenCodeAdapter
from .tools.pi_adapter import PiAdapter
from .tools.shell_tools import ShellTools
from .tools.host_adapter import HostAdapter, HostTurnRequired, prompt_key
from .tools.base import BaseAdapter

__version__ = "1.0.0"

__all__ = [
    "ReviewSkill", "ReviseSkill", "ExploreSkill", "VerifySkill",
    "LoopSkill", "AnalyzeSkill", "GenerateSkill",
    "ReviewerSubAgent", "ReviewReport", "Defect", "Finding",
    "DECISION_CHOICES", "DECISION_APPROVING", "DECISION_REJECTING",
    "ReviserSubAgent", "RevisedPR",
    "ExplorerSubAgent", "ExplorationResult",
    "VerifierSubAgent", "VerificationResult",
    "AnalyzerSubAgent", "AnalyzerResult",
    "GeneratorSubAgent", "GeneratedPR",
    "LoopSubAgent", "LoopResult", "LoopIteration",
    "ClaudeCodeAdapter", "CursorAdapter", "OpenCodeAdapter", "PiAdapter", "ShellTools", "BaseAdapter",
    "HostAdapter", "HostTurnRequired", "prompt_key",
]
