"""Agents: specialised workers with typed output contracts."""

from .base import Agent, AgentFailure, AgentResult
from .coder import CoderAgent
from .contracts import (
    CodeSubmission,
    ContractError,
    SourceFile,
    extract_json,
)

__all__ = [
    "Agent",
    "AgentResult",
    "AgentFailure",
    "CoderAgent",
    "CodeSubmission",
    "ContractError",
    "SourceFile",
    "extract_json",
]
