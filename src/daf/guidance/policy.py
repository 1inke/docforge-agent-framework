"""Source scope policy evaluation for local single-user projects.

This module never imports ContextManager so low-level readers can use it directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Literal

from daf.core.contracts import ProjectWorkspaceState
from daf.core.errors import SourceScopeDeniedError


def canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


class SourcePolicyEvaluator:
    """Evaluate Discover/Read/Cite permissions against the project's source policy."""

    @staticmethod
    def revision(state: ProjectWorkspaceState) -> int:
        return state.source_policy.revision

    @staticmethod
    def allowed_ids(
        state: ProjectWorkspaceState,
        operation: Literal["discover", "read", "cite"] = "read",
    ) -> set[str]:
        if operation not in {"discover", "read", "cite"}:
            raise ValueError(f"Unknown source operation: {operation}")
        policy = state.source_policy
        all_ids = set(state.source_registry.documents)
        if policy.allowed_source_ids is None:
            return all_ids
        allowed = set(policy.allowed_source_ids)
        if policy.mode != "project_only":
            allowed.update(policy.approved_external_source_ids)
        return allowed & all_ids

    @classmethod
    def permits(
        cls, state: ProjectWorkspaceState, source_id: str, operation: str = "read"
    ) -> bool:
        return source_id in cls.allowed_ids(state, operation)  # type: ignore[arg-type]

    @classmethod
    def assert_source(
        cls, state: ProjectWorkspaceState, source_id: str, operation: str = "read"
    ) -> None:
        if not cls.permits(state, source_id, operation):
            raise SourceScopeDeniedError(
                "Current project source policy denies this operation. Ask the user to update source scope.",
                details={
                    "operation": operation,
                    "policy_revision": cls.revision(state),
                },
            )

    @classmethod
    def assert_evidence(
        cls, state: ProjectWorkspaceState, evidence_ids: Iterable[str]
    ) -> None:
        for evidence_id in evidence_ids:
            evidence = state.evidence_ledger.items.get(evidence_id)
            if evidence is not None:
                cls.assert_source(state, evidence.source_id, "cite")

    @classmethod
    def assert_section(cls, state: ProjectWorkspaceState, section_id: str) -> None:
        section = state.section_versions.sections.get(section_id)
        if section:
            cls.assert_evidence(
                state, (b.evidence_id for b in section.source_bindings if b.evidence_id)
            )

    @classmethod
    def assert_document(cls, state: ProjectWorkspaceState) -> None:
        for node in state.outline_graph.nodes:
            cls.assert_evidence(state, node.assigned_evidence_ids)
        for sid in state.section_versions.sections:
            cls.assert_section(state, sid)

    @classmethod
    def public_policy(cls, state: ProjectWorkspaceState) -> dict[str, Any]:
        policy = state.source_policy
        return {
            "configuration_status": "configured",
            "revision": cls.revision(state),
            "mode": policy.mode,
            "allowed_source_ids": sorted(cls.allowed_ids(state)),
            "permissions": {
                op: sorted(cls.allowed_ids(state, op))
                for op in ("discover", "read", "cite")
            },
            "external_lookup_authorized": bool(
                policy.external_lookup_enabled and policy.mode != "project_only"
            ),
            "external_lookup_available": False,
            "external_lookup_status": "external_lookup_unavailable",
            "citation_requires_registered_evidence": True,
        }

    @classmethod
    def context_view(cls, state: ProjectWorkspaceState) -> ProjectWorkspaceState:
        """Return a filtered state view containing only allowed sources and evidence."""
        allowed = cls.allowed_ids(state)
        if allowed == set(state.source_registry.documents):
            return state
        view = state.model_copy(deep=True)
        view.source_registry.documents = {
            sid: doc
            for sid, doc in view.source_registry.documents.items()
            if sid in allowed
        }
        view.source_registry.slices = {
            sid: sl
            for sid, sl in view.source_registry.slices.items()
            if sl.source_id in allowed
        } if view.source_registry.slices else {}
        view.evidence_ledger.items = {
            eid: ev
            for eid, ev in view.evidence_ledger.items.items()
            if ev.source_id in allowed
        }
        return view
