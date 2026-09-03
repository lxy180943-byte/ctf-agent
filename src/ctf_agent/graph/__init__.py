"""Experimental LangGraph workflow primitives."""

from ctf_agent.graph.checkpoint import graph_thread_id, open_run_checkpointer
from ctf_agent.graph.context import (
    EvidenceFact,
    EvidenceHypothesis,
    EvidenceObservation,
    EvidencePacket,
    build_evidence_packet,
)
from ctf_agent.graph.evidence_delta import EvidenceDelta, EvidenceProvenance, derive_evidence_delta
from ctf_agent.graph.experiment_policy import (
    ExperimentAssessment,
    ExperimentFingerprint,
    assess_experiment,
    fingerprint_experiment,
)
from ctf_agent.graph.state import WorkflowState, initial_workflow_state


def build_workflow():
    """Lazily import LangGraph so state/resume helpers stay independently usable."""

    from ctf_agent.graph.builder import build_workflow as compile_workflow

    return compile_workflow()


__all__ = [
    "EvidenceDelta",
    "EvidenceFact",
    "EvidenceHypothesis",
    "EvidenceObservation",
    "EvidencePacket",
    "EvidenceProvenance",
    "ExperimentAssessment",
    "ExperimentFingerprint",
    "WorkflowState",
    "assess_experiment",
    "build_evidence_packet",
    "build_workflow",
    "derive_evidence_delta",
    "fingerprint_experiment",
    "graph_thread_id",
    "initial_workflow_state",
    "open_run_checkpointer",
]
