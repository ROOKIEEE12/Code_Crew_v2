"""Agents: specialised workers with typed output contracts."""

from .architect import ArchitectAgent
from .base import Agent, AgentFailure, AgentResult, ContextViolation
from .coder import CoderAgent
from .contracts import (
    CodeSubmission,
    ContractError,
    Diagnosis,
    Example,
    FunctionSpec,
    PlanDependency,
    PlanModule,
    ProjectPlan,
    SourceFile,
    Spec,
    extract_json,
    parse_file_list,
)
from .diagnostician import DiagnosticianAgent
from .implementer import ImplementerAgent
from .patcher import PatcherAgent, SpecRepairAgent
from .spec_agent import SpecAgent
from .tester import TestAuthorAgent

__all__ = [
    "Agent",
    "AgentResult",
    "AgentFailure",
    "ContextViolation",
    "ArchitectAgent",
    "PlanDependency",
    "PlanModule",
    "ProjectPlan",
    "CoderAgent",
    "DiagnosticianAgent",
    "PatcherAgent",
    "SpecRepairAgent",
    "SpecAgent",
    "ImplementerAgent",
    "TestAuthorAgent",
    "CodeSubmission",
    "ContractError",
    "Diagnosis",
    "Example",
    "FunctionSpec",
    "SourceFile",
    "Spec",
    "extract_json",
    "parse_file_list",
]
