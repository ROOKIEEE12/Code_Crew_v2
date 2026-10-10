"""Agents: specialised workers with typed output contracts."""

from .base import Agent, AgentFailure, AgentResult, ContextViolation
from .coder import CoderAgent
from .contracts import (
    CodeSubmission,
    ContractError,
    Example,
    FunctionSpec,
    SourceFile,
    Spec,
    extract_json,
    parse_file_list,
)
from .implementer import ImplementerAgent
from .spec_agent import SpecAgent
from .tester import TestAuthorAgent

__all__ = [
    "Agent",
    "AgentResult",
    "AgentFailure",
    "ContextViolation",
    "CoderAgent",
    "SpecAgent",
    "ImplementerAgent",
    "TestAuthorAgent",
    "CodeSubmission",
    "ContractError",
    "Example",
    "FunctionSpec",
    "SourceFile",
    "Spec",
    "extract_json",
    "parse_file_list",
]
