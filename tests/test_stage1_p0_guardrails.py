"""Stage 1 (P0-1, P0-2, P0-3) guardrail acceptance tests for local single-user DAF."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from daf import DAFEngine
from daf.core.errors import ContractValidationError, ReviewChecklistIncompleteError
from daf.interfaces.cli.main import run_cli_args
from daf.interfaces.local_ui.app import create_local_ui_app


def _write_yaml_profile(workspace_root: Path, profile_id: str = "demo_yaml") -> None:
    prof_dir = workspace_root / "profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    (prof_dir / f"{profile_id}.yaml").write_text(
        f"""profile_id: {profile_id}
display_name: YAML Test Profile
sections:
  - id: sec_a
    title: 1. Overview
    required: true
    words: [10, 1000]
    review_checklist:
      - Are key metrics grounded in cited evidence?
content:
  forbidden_phrases: [TODO]
layout:
  cjk_font: 宋体
  body_font_size_pt: 12
""",
        encoding="utf-8",
    )


def _setup_project_with_evidence(tmp_path: Path) -> tuple[DAFEngine, Path, str]:
    ws = tmp_path / "ws"
    _write_yaml_profile(ws, "demo_yaml")
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    src_file = src_dir / "bench.md"
    src_file.write_text(
        "# Benchmark\nBaseline latency is 18.4 ms across 15 nodes, and optimized latency is 9.2 ms.\n",
        encoding="utf-8",
    )
    engine = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    engine.create_project(project_id="p1", title="P0 Guardrails Doc", profile_id="demo_yaml")
    ing = engine.ingest_sources(project_id="p1", source_paths=[str(src_file)])
    src_id = ing["ingested_sources"][0]["source_id"]
    sl = engine.list_source_blocks(project_id="p1", source_id=src_id)["blocks"][0]
    ev_res = engine.register_evidence(
        project_id="p1",
        evidence_items=[
            {
                "evidence_id": "ev_01",
                "source_id": src_id,
                "slice_id": sl["slice_id"],
                "locator": sl["locator"],
                "quote_excerpt": "Baseline latency is 18.4 ms across 15 nodes, and optimized latency is 9.2 ms.",
                "source_reliability": "trusted",  # Agent attempt to self-mark trusted must be downgraded
            }
        ],
    )
    assert ev_res["evidence_items"][0]["source_reliability"] == "unassessed"

    engine.submit_outline(
        project_id="p1",
        global_thesis="Verify inline citation grounding and guardrails.",
        nodes=[
            {
                "section_id": "sec_a",
                "title": "1. Overview",
                "writing_objectives": ["Report benchmark results accurately."],
                "assigned_evidence_ids": ["ev_01"],
            }
        ],
    )
    req = engine.request_approval(project_id="p1", artifact_type="outline")
    engine.decide_approval_by_human(
        project_id="p1",
        approval_request_id=req["approval_request_id"],
        decision="approved",
        confirmation_channel="local_cli",
    )
    return engine, ws, src_id


def test_p0_1_inline_citation_checks_ra_rb_rc_rd(tmp_path: Path) -> None:
    engine, _ws, _src_id = _setup_project_with_evidence(tmp_path)

    # R-C: nonexistent evidence rejects submission
    ctx = engine.get_section_context(project_id="p1", section_id="sec_a", action="draft")
    with pytest.raises(ContractValidationError) as exc_rc:
        engine.submit_section(
            {
                "project_id": "p1",
                "section_id": "sec_a",
                "base_revision": 0,
                "context_revision": ctx["context_revision"],
                "context_fingerprint": ctx["context_fingerprint"],
                "idempotency_key": "rc-1",
                "author_session": "author-A",
                "content_markdown": "Latency dropped to 9.2 ms[@ev_missing].",
                "summary": "Missing evidence test.",
            }
        )
    assert exc_rc.value.code == "EVIDENCE_UNVERIFIED"

    # R-B mismatch (99.9 vs 18.4), R-B boundary (5 vs 15), R-A uncited factual claim, R-D false experiment in hypothesis
    body = (
        "Optimized latency is 99.9 ms[@ev_01]. "
        "Cluster size was 5 nodes[@ev_01]. "
        "时延降低 50%。 "
        "实验表明该结构可扩展至百万节点[@hypothesis]。"
    )
    res = engine.submit_section(
        {
            "project_id": "p1",
            "section_id": "sec_a",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "ra-rb-rd-1",
            "author_session": "author-A",
            "content_markdown": body,
            "summary": "Combined L2a check test.",
        }
    )
    assert len(res["uncited_claim_issues"]) >= 1
    assert any(i["category"] == "UNCITED_FACTUAL_CLAIM" for i in res["uncited_claim_issues"])
    warnings = res["l2_claim_support_warnings"]
    assert len(warnings) == 3  # 99.9 mismatch, 5 boundary mismatch vs 15, hypothesis false marker
    assert all(w["rule_check_status"] == "rule_failed" for w in warnings)

    # Removing citation on revision 2 removes binding and stales existing review tasks
    review_res = engine.review_document(project_id="p1")
    task_id = review_res["semantic_review_tasks"][0]["task_id"]
    ctx2 = engine.get_section_context(project_id="p1", section_id="sec_a", action="revise")
    res2 = engine.submit_section(
        {
            "project_id": "p1",
            "section_id": "sec_a",
            "base_revision": 1,
            "context_revision": ctx2["context_revision"],
            "context_fingerprint": ctx2["context_fingerprint"],
            "idempotency_key": "remove-cite-2",
            "author_session": "author-A",
            "content_markdown": "This section introduces the architecture overview in plain narrative prose.",
            "summary": "Removed citations.",
        }
    )
    assert res2["source_bindings"] == []
    tasks_after = engine.list_semantic_review_tasks(project_id="p1")["tasks"]
    stale_task = next(t for t in tasks_after if t["task_id"] == task_id)
    assert stale_task["status"] == "stale"


def test_p0_2_profile_lock_agent_note_and_independent_review(tmp_path: Path) -> None:
    engine, _ws, _src_id = _setup_project_with_evidence(tmp_path)

    # P0-2A: Agent cannot call update_profile_settings or pass profile_overrides
    with pytest.raises(ContractValidationError):
        engine.call_agent_tool("update_profile_settings", {"project_id": "p1", "profile_overrides": {}})
    with pytest.raises(TypeError):
        engine.call_agent_tool(
            "create_project",
            {"project_id": "p2", "title": "Bad", "profile_overrides": {"approval_policy": []}},
        )

    # P0-2B: Agent can only record_agent_note, cannot write acknowledged_template_conflicts
    with pytest.raises(ContractValidationError):
        engine.call_agent_tool(
            "submit_feedback",
            {"project_id": "p1", "feedback_text": "x", "acknowledge_template_conflicts": ["c1"]},
        )
    note_res = engine.call_agent_tool(
        "record_agent_note",
        {"project_id": "p1", "note_text": "User mentioned keeping section concise."},
    )
    assert note_res["source"] == "agent_note"
    assert engine.store.load_state("p1").decision_log.acknowledged_template_conflicts == []

    # Submit valid section with author_session='author-A'
    ctx = engine.get_section_context(project_id="p1", section_id="sec_a", action="draft")
    engine.submit_section(
        {
            "project_id": "p1",
            "section_id": "sec_a",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "draft-valid-1",
            "author_session": "author-A",
            "content_markdown": "Our benchmark shows baseline latency is 18.4 ms across 15 nodes and optimized latency is 9.2 ms[@ev_01].",
            "summary": "Grounded draft.",
        }
    )
    rev = engine.review_document(project_id="p1")
    task = rev["semantic_review_tasks"][0]

    # P0-2C: Author session cannot claim own section review
    with pytest.raises(ContractValidationError) as exc_self:
        engine.claim_semantic_review_task(project_id="p1", task_id=task["task_id"], reviewer_id="author-A")
    assert exc_self.value.code == "REVIEWER_IS_AUTHOR"

    # Independent reviewer claims
    claimed = engine.claim_semantic_review_task(project_id="p1", task_id=task["task_id"], reviewer_id="reviewer-B")
    sec_bindings = engine.store.load_state("p1").section_versions.sections["sec_a"].source_bindings

    # Quote not in section body must be rejected
    with pytest.raises(ReviewChecklistIncompleteError):
        engine.submit_semantic_review(
            project_id="p1",
            task_id=task["task_id"],
            section_revision=claimed["target_section_revision"],
            context_fingerprint=claimed["target_context_fingerprint"],
            findings=[],
            review_summary="Attempt with hallucinated quote.",
            recommendation="approve",
            reviewed_by="reviewer-B",
            claim_evaluations=[
                {
                    "binding_id": sec_bindings[0].binding_id,
                    "semantic_support_status": "supported",
                    "rationale": "Exact match.",
                }
            ],
            checklist_results=[
                {
                    "check_id": item["check_id"],
                    "question": item["question"],
                    "verdict": "pass",
                    "rationale": "Verified metrics against benchmark excerpt in section.",
                    "quotes": ["This fabricated sentence does not exist in section text."],
                }
                for item in claimed["checklist_items"]
            ],
            checklist_hash=claimed["checklist_hash"],
        )

    # Valid quote -> submit-is-accept directly transitions to 'accepted'
    submitted = engine.submit_semantic_review(
        project_id="p1",
        task_id=task["task_id"],
        section_revision=claimed["target_section_revision"],
        context_fingerprint=claimed["target_context_fingerprint"],
        findings=[],
        review_summary="Verified against section quotes and source excerpt.",
        recommendation="approve",
        reviewed_by="reviewer-B",
        claim_evaluations=[
            {
                "binding_id": sec_bindings[0].binding_id,
                "semantic_support_status": "supported",
                "rationale": "Exact numeric match with source excerpt.",
            }
        ],
        checklist_results=[
            {
                "check_id": item["check_id"],
                "question": item["question"],
                "verdict": "pass",
                "rationale": f"Verified [{item['check_id']}:{item['question'][:20]}] against benchmark excerpt.",
                "quotes": ["baseline latency is 18.4 ms across 15 nodes and optimized latency is 9.2 ms"],
                "evidence_ids": ["ev_01"],
            }
            for item in claimed["checklist_items"]
        ],
        checklist_hash=claimed["checklist_hash"],
    )
    assert submitted["status"] == "accepted"
    with pytest.raises(ContractValidationError):
        engine.call_agent_tool("accept_semantic_review", {"project_id": "p1", "task_id": task["task_id"]})


def test_p0_3_local_ui_token_and_stale_hash_and_cli_approvals(tmp_path: Path) -> None:
    engine, ws, _src_id = _setup_project_with_evidence(tmp_path)

    # Draft + review so we can request final_draft approval
    ctx = engine.get_section_context(project_id="p1", section_id="sec_a", action="draft")
    engine.submit_section(
        {
            "project_id": "p1",
            "section_id": "sec_a",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "ui-draft-1",
            "author_session": "author-A",
            "content_markdown": "Baseline latency is 18.4 ms across 15 nodes and optimized latency is 9.2 ms[@ev_01].",
            "summary": "Initial draft.",
        }
    )
    rev = engine.review_document(project_id="p1")
    task = rev["semantic_review_tasks"][0]
    claimed = engine.claim_semantic_review_task(project_id="p1", task_id=task["task_id"], reviewer_id="reviewer-B")
    sec_bindings = engine.store.load_state("p1").section_versions.sections["sec_a"].source_bindings
    engine.submit_semantic_review(
        project_id="p1",
        task_id=task["task_id"],
        section_revision=claimed["target_section_revision"],
        context_fingerprint=claimed["target_context_fingerprint"],
        findings=[],
        review_summary="Verified.",
        recommendation="approve",
        reviewed_by="reviewer-B",
        claim_evaluations=[
            {
                "binding_id": sec_bindings[0].binding_id,
                "semantic_support_status": "supported",
                "rationale": "Exact match.",
            }
        ],
        checklist_results=[
            {
                "check_id": item["check_id"],
                "question": item["question"],
                "verdict": "pass",
                "rationale": f"Verified [{item['check_id']}:{item['question'][:20]}] metrics against live excerpt.",
                "quotes": ["Baseline latency is 18.4 ms across 15 nodes"],
                "evidence_ids": ["ev_01"],
            }
            for item in claimed["checklist_items"]
        ],
        checklist_hash=claimed["checklist_hash"],
    )

    final_req = engine.request_approval(project_id="p1", artifact_type="final_draft")
    req_id = final_req["approval_request_id"]
    old_hash = final_req["artifact_hash"]

    app, token = create_local_ui_app(workspace_root=ws)
    client = TestClient(app)

    # Without token -> 403
    assert client.get("/").status_code == 403
    assert client.get(f"/projects/p1").status_code == 403

    # With token -> 200 and contains artifact hash
    page = client.get(f"/projects/p1?token={token}")
    assert page.status_code == 200
    assert old_hash in page.text

    # CLI approvals list & show work without HMAC
    listed = run_cli_args(["--workspace", str(ws), "approvals", "list", "--project-id", "p1"])
    assert any(r["approval_request_id"] == req_id for r in listed["requests"])
    shown = run_cli_args(["--workspace", str(ws), "approvals", "show", req_id, "--project-id", "p1"])
    assert shown["request"]["artifact_hash"] == old_hash

    # Stale hash rejection when page-loaded hash mismatches
    stale_resp = client.post(
        f"/projects/p1/approvals/{req_id}/decide?token={token}",
        data={"decision": "approved", "expected_artifact_hash": "sha256:stale_hash"},
    )
    assert stale_resp.status_code == 409
    assert stale_resp.json()["error"]["code"] == "APPROVAL_STALE"

    # Valid approval via local UI sets confirmed_via='local_ui'
    ok_resp = client.post(
        f"/projects/p1/approvals/{req_id}/decide?token={token}",
        data={"decision": "approved", "expected_artifact_hash": old_hash},
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["data"]["approval_record"]["confirmed_via"] == "local_ui"
