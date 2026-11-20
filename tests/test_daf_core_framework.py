"""Template-agnostic core framework acceptance tests for DocForge Agent Framework (DAF)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from daf import DAFEngine
from daf.core.contracts import (
    ContentProfile,
    DocumentProfileContract,
    LayoutProfile,
    SectionRule,
    StructureProfile,
)
from daf.core.errors import (
    BibliographyMissingError,
    ProjectAlreadyExistsError,
    SecurityBoundaryError,
    SourcePolicyChangeRequiresHumanError,
    UntrustedApprovalChannelError,
)
from daf.document_profiles import (
    clear_custom_profiles,
    get_document_profile,
    register_document_profile,
)
from daf.guidance.profile_guidance import ProfileGuidanceRegistry, ProfileMethods
from daf.interfaces.cli.main import run_cli_args
from daf.interfaces.http_api.app import create_http_app
from daf.interfaces.mcp_server.server import create_mcp_server


def _build_custom_profile(profile_id: str = "custom_spec") -> DocumentProfileContract:
    return DocumentProfileContract(
        profile_id=profile_id,
        display_name="Custom Structured Document",
        description="Runtime-registered custom profile for testing.",
        content_profile=ContentProfile(
            discourse_style="technical",
            evidence_density="high",
            tone_constraints=["Keep claims grounded in verified evidence."],
            forbidden_phrases=["TODO"],
            terminology_consistency_required=True,
        ),
        structure_profile=StructureProfile(
            required_sections=[
                SectionRule(
                    section_id="sec_overview",
                    title="1. Overview",
                    required=True,
                    min_words=20,
                    max_words=1000,
                    depends_on=[],
                    required_elements=["Problem statement"],
                    review_checklist=["Is the overview grounded in source evidence?"],
                ),
                SectionRule(
                    section_id="sec_method",
                    title="2. Method",
                    required=True,
                    min_words=20,
                    max_words=1000,
                    depends_on=["sec_overview"],
                    required_elements=["Approach description"],
                    review_checklist=["Does the method align with the overview?"],
                ),
            ],
            optional_sections=[],
        ),
        layout_profile=LayoutProfile(),
        approval_policy=["outline", "final_draft"],
        block_release_on_major_issues=True,
    )


def test_default_neutral_profile_and_dynamic_registration(tmp_path: Path) -> None:
    try:
        engine = DAFEngine(workspace_root=tmp_path / "ws")
        caps = engine.get_capabilities(profile_id="default")
        assert caps["requested_profile"]["profile_id"] == "default"
        assert caps["requested_profile"]["structure_profile"]["required_sections"] == []

        custom = _build_custom_profile("runtime_profile")
        register_document_profile(custom)
        ProfileGuidanceRegistry.register(
            "runtime_profile",
            ProfileMethods("1.0", {"plan_outline": ("Custom outline method.",)}),
        )
        loaded = get_document_profile("runtime_profile")
        assert loaded.profile_id == "runtime_profile"
        assert len(loaded.structure_profile.required_sections) == 2

        resolved = ProfileGuidanceRegistry.resolve("runtime_profile", "plan_outline")
        assert resolved["methods"] == ["Custom outline method."]
    finally:
        clear_custom_profiles()
        ProfileGuidanceRegistry.unregister("runtime_profile")


def test_create_only_enforced(tmp_path: Path) -> None:
    engine = DAFEngine(workspace_root=tmp_path / "ws")
    created = engine.create_project(project_id="proj_01", title="Neutral Doc")
    assert created["profile_id"] == "default"
    assert created["require_outline_approval"] is True
    assert created["require_final_approval"] is True

    with pytest.raises(ProjectAlreadyExistsError):
        engine.create_project(project_id="proj_01", title="Duplicate")


def test_source_import_boundary_denies_unconfigured_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAF_ALLOWED_SOURCE_ROOTS", raising=False)
    src_file = tmp_path / "note.md"
    src_file.write_text("# Note\nMeasured throughput reached 42.5 ops/s.\n", encoding="utf-8")

    engine = DAFEngine(workspace_root=tmp_path / "ws")
    engine.create_project(project_id="proj_sec", title="Boundary Check")
    with pytest.raises(SecurityBoundaryError) as exc_info:
        engine.ingest_sources(project_id="proj_sec", source_paths=[str(src_file)])
    assert exc_info.value.code == "SOURCE_IMPORT_NOT_CONFIGURED"


def test_agent_domain_separation_blocks_privileged_calls(tmp_path: Path) -> None:
    engine = DAFEngine(workspace_root=tmp_path / "ws")
    engine.create_project(project_id="proj_guard", title="Guard Test")

    with pytest.raises(UntrustedApprovalChannelError):
        engine.call_agent_tool("decide_approval_by_human", {"project_id": "proj_guard"})

    with pytest.raises(SourcePolicyChangeRequiresHumanError):
        engine.call_agent_tool("set_source_policy_by_user", {"project_id": "proj_guard"})


def test_end_to_end_template_agnostic_workflow(tmp_path: Path) -> None:
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    src_file = src_dir / "data.md"
    src_file.write_text(
        "# Experiment Data\nThe baseline latency is 18.4 ms and the new pipeline reduces latency to 9.2 ms.\n",
        encoding="utf-8",
    )

    try:
        register_document_profile(_build_custom_profile("custom_e2e"))
        engine = DAFEngine(
            workspace_root=tmp_path / "ws",
            allowed_source_roots=[src_dir],
        )
        engine.create_project(
            project_id="proj_e2e",
            title="End-to-End Neutral Document",
            profile_id="custom_e2e",
            domain="engineering",
        )

        ingested = engine.ingest_sources(project_id="proj_e2e", source_paths=[str(src_file)])
        source_id = ingested["ingested_sources"][0]["source_id"]

        blocks = engine.list_source_blocks(project_id="proj_e2e", source_id=source_id)
        slice_info = blocks["blocks"][0]
        read_res = engine.get_source_block(
            project_id="proj_e2e",
            slice_id=slice_info["slice_id"],
            expected_sha256=slice_info["sha256"],
        )
        assert "9.2 ms" in read_res["text"]

        ev_res = engine.register_evidence(
            project_id="proj_e2e",
            evidence_items=[
                {
                    "evidence_id": "ev_01",
                    "source_id": source_id,
                    "slice_id": slice_info["slice_id"],
                    "locator": slice_info["locator"],
                    "quote_excerpt": "The baseline latency is 18.4 ms and the new pipeline reduces latency to 9.2 ms.",
                    "claim_summary": "New pipeline reduces latency from 18.4 ms to 9.2 ms.",
                }
            ],
        )
        assert ev_res["evidence_items"][0]["excerpt_match_status"] == "matched"

        outline_guidance = engine.get_task_guidance("proj_e2e", "plan_outline")
        assert outline_guidance["task_status"] != "completed"

        engine.submit_outline(
            project_id="proj_e2e",
            global_thesis="Demonstrate latency reduction using verifiable evidence.",
            nodes=[
                {
                    "section_id": "sec_overview",
                    "title": "1. Overview",
                    "writing_objectives": ["Summarize the baseline latency problem."],
                    "depends_on": [],
                    "assigned_evidence_ids": ["ev_01"],
                },
                {
                    "section_id": "sec_method",
                    "title": "2. Method",
                    "writing_objectives": ["Explain the pipeline design that lowers latency."],
                    "depends_on": ["sec_overview"],
                    "assigned_evidence_ids": ["ev_01"],
                },
            ],
        )

        outline_req = engine.request_approval(project_id="proj_e2e", artifact_type="outline")
        engine.decide_approval_by_human(
            project_id="proj_e2e",
            approval_request_id=outline_req["approval_request_id"],
            decision="approved",
            decided_by="local_user",
            confirmation_channel="local_cli",
        )

        section_bodies = [
            (
                "sec_overview",
                "This overview defines the latency bottleneck in our system. "
                "Empirical measurements show that the baseline latency is 18.4 ms under steady load[@ev_01].",
                "baseline latency is 18.4 ms",
            ),
            (
                "sec_method",
                "Our method streamlines request batching and removes redundant serialization steps. "
                "Compared with the 18.4 ms baseline, the new pipeline reduces latency to 9.2 ms[@ev_01].",
                "new pipeline reduces latency to 9.2 ms",
            ),
        ]
        for sec_id, body, _q in section_bodies:
            ctx = engine.get_section_context(project_id="proj_e2e", section_id=sec_id, action="draft")
            engine.submit_section(
                {
                    "project_id": "proj_e2e",
                    "section_id": sec_id,
                    "base_revision": 0,
                    "context_revision": ctx["context_revision"],
                    "context_fingerprint": ctx["context_fingerprint"],
                    "idempotency_key": f"draft-{sec_id}-v1",
                    "author_session": "writer-1",
                    "content_markdown": body,
                    "summary": f"Draft for {sec_id}.",
                }
            )

        quote_by_sec = {s: q for s, _b, q in section_bodies}
        review_res = engine.review_document(project_id="proj_e2e")
        for task in review_res["semantic_review_tasks"]:
            claimed = engine.claim_semantic_review_task(
                project_id="proj_e2e",
                task_id=task["task_id"],
                reviewer_id="reviewer-2",
            )
            sec_id = claimed["section_id"]
            bindings = engine.store.load_state("proj_e2e").section_versions.sections[sec_id].source_bindings
            checklist_results = [
                {
                    "check_id": item["check_id"],
                    "question": item["question"],
                    "verdict": "pass",
                    "rationale": "Verified against current section text and evidence.",
                    "quotes": [quote_by_sec[sec_id]],
                    "section_locations": [sec_id],
                    "evidence_ids": ["ev_01"],
                }
                for item in claimed["checklist_items"]
            ]
            claim_evals = [
                {
                    "binding_id": b.binding_id,
                    "semantic_support_status": "supported",
                    "rationale": "Exact numeric match with source excerpt.",
                }
                for b in bindings
            ]
            sub = engine.submit_semantic_review(
                project_id="proj_e2e",
                task_id=task["task_id"],
                section_revision=claimed["target_section_revision"],
                context_fingerprint=claimed["target_context_fingerprint"],
                findings=[],
                review_summary="All checklist items and claims verified.",
                recommendation="approve",
                reviewed_by="reviewer-2",
                claim_evaluations=claim_evals,
                checklist_results=checklist_results,
                checklist_hash=claimed["checklist_hash"],
            )
            assert sub["status"] == "accepted"

        final_req = engine.request_approval(project_id="proj_e2e", artifact_type="final_draft")
        engine.decide_approval_by_human(
            project_id="proj_e2e",
            approval_request_id=final_req["approval_request_id"],
            decision="approved",
            decided_by="local_user",
            confirmation_channel="local_cli",
        )

        with pytest.raises(BibliographyMissingError):
            engine.render_document(project_id="proj_e2e", purpose="release")

        prop = engine.call_agent_tool(
            "propose_source_bibliography",
            {
                "project_id": "proj_e2e",
                "source_id": source_id,
                "csl_json": {
                    "type": "article-journal",
                    "title": "Latency Reduction Pipeline Evaluation",
                    "author": [{"family": "Zhang", "given": "San"}],
                    "issued": {"date-parts": [[2025]]},
                },
            },
        )
        assert prop["confirmed"] is False
        with pytest.raises(SourcePolicyChangeRequiresHumanError):
            engine.call_agent_tool(
                "adopt_source_bibliography_by_user",
                {"project_id": "proj_e2e", "source_id": source_id},
            )
        engine.adopt_source_bibliography_by_user(project_id="proj_e2e", source_id=source_id)

        render_res = engine.render_document(project_id="proj_e2e", purpose="release")
        assert Path(render_res["artifact_path"]).exists()

        export_res = engine.export_document(project_id="proj_e2e", purpose="release", export_pdf=False)
        assert export_res["is_official_release"] is True
        assert Path(export_res["export_bundle_json_path"]).exists()
    finally:
        clear_custom_profiles()


def test_cli_http_and_mcp_interfaces_smoke(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    cli_caps = run_cli_args(["--workspace", str(ws), "get-capabilities", "--profile-id", "default"])
    assert cli_caps["requested_profile"]["profile_id"] == "default"

    cli_created = run_cli_args(
        [
            "--workspace",
            str(ws),
            "create-project",
            "--project-id",
            "cli_proj",
            "--title",
            "CLI Project",
        ]
    )
    assert cli_created["profile_id"] == "default"

    app = create_http_app(workspace_root=ws)
    client = TestClient(app)
    resp = client.get("/api/v1/projects/cli_proj/status")
    assert resp.status_code == 200
    assert resp.json()["project_id"] == "cli_proj"

    mcp = create_mcp_server(workspace_root=ws)
    assert mcp is not None
    status_res = run_cli_args(
        [
            "--workspace",
            str(ws),
            "call",
            "get_project_status",
            "--params",
            json.dumps({"project_id": "cli_proj"}),
        ]
    )
    assert status_res["project_id"] == "cli_proj"
