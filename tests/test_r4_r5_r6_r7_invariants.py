"""Round-2 R5, R6, R7-A performance, FastAPI HTTP/UI & R8 code-hygiene invariants."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time
import tomllib

from fastapi.testclient import TestClient
import pytest

import daf
from daf import DAFEngine
from daf.capability_registry.registry import PROTOCOL_VERSION
from daf.core.contracts import SemanticReviewTask, SourceBinding
from daf.core.errors import ContractValidationError, ReviewChecklistIncompleteError
from daf.interfaces.http_api.app import create_http_app
from daf.interfaces.local_ui.app import create_local_ui_app

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DAF = REPO_ROOT / "src" / "daf"
TESTS_DIR = REPO_ROOT / "tests"


def test_r5_mandatory_identities_quotes_and_rationales(tmp_path: Path) -> None:
    """E2(d) regression: missing author/reviewer IDs, 4-char quotes, 'ok' rationales,
    and >3 checklist items quoting one sentence are all rejected."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "d.md").write_text("The baseline latency is 18.4 ms across 15 nodes.\n", encoding="utf-8")
    ws = tmp_path / "ws"
    prof = ws / "profiles"
    prof.mkdir(parents=True)
    (prof / "strict4.yaml").write_text(
        """profile_id: strict4
display_name: Four Question Profile
sections:
  - id: s1
    title: S1
    required: true
    words: [5, 2000]
    review_checklist:
      - 问题一：核心测量指标是否可溯源？
      - 问题二：关键结论是否明确给出？
      - 问题三：复杂度讨论是否完整？
      - 问题四：可复现性细节是否交代？
""",
        encoding="utf-8",
    )

    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_r5", "title": "R5 Review Independence", "profile_id": "strict4"})
    ing = A("ingest_sources", {"project_id": "p_r5", "source_paths": [str(src_dir / "d.md")]})
    sid = ing["ingested_sources"][0]["source_id"]
    sl = A("list_source_blocks", {"project_id": "p_r5", "source_id": sid})["blocks"][0]
    A("register_evidence", {"project_id": "p_r5", "evidence_items": [{"evidence_id": "ev1", "source_id": sid, "slice_id": sl["slice_id"], "quote_excerpt": "The baseline latency is 18.4 ms across 15 nodes"}]})
    A("submit_outline", {"project_id": "p_r5", "global_thesis": "t", "nodes": [{"section_id": "s1", "title": "S1"}]})
    oreq = A("request_approval", {"project_id": "p_r5", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_r5", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")
    ctx = A("get_section_context", {"project_id": "p_r5", "section_id": "s1", "action": "draft"})

    # Missing or too-short author_session rejected
    for bad_author in ("", "ab"):
        with pytest.raises(ContractValidationError):
            A(
                "submit_section",
                {
                    "project_id": "p_r5",
                    "section_id": "s1",
                    "base_revision": 0,
                    "context_revision": ctx["context_revision"],
                    "context_fingerprint": ctx["context_fingerprint"],
                    "idempotency_key": f"bad-{bad_author}",
                    "author_session": bad_author,
                    "content_markdown": "基线时延为 18.4 ms[@ev1]。",
                    "summary": "bad author",
                },
            )

    A(
        "submit_section",
        {
            "project_id": "p_r5",
            "section_id": "s1",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "r5-good-draft",
            "author_session": "writer-01",
            "content_markdown": "基线时延为 18.4 ms[@ev1]。结论成立。第三句补充复杂度分析内容。第四句讨论可复现性保障方案。",
            "summary": "Valid draft",
        },
    )

    # Missing reviewer_id rejected
    with pytest.raises(ContractValidationError) as exc_noid:
        A("start_review", {"project_id": "p_r5"})
    assert exc_noid.value.code == "REVIEWER_ID_REQUIRED"

    # Same author_session rejected
    with pytest.raises(ContractValidationError) as exc_same:
        A("start_review", {"project_id": "p_r5", "reviewer_id": "writer-01"})
    assert exc_same.value.code == "REVIEWER_IS_AUTHOR"

    rv = A("start_review", {"project_id": "p_r5", "reviewer_id": "reviewer-02"})
    t = rv["claimed_tasks"][0]
    st = eng.store.load_state("p_r5")
    ce = [{"binding_id": b.binding_id, "semantic_support_status": "supported", "rationale": "Exact numeric figure matches excerpt."} for b in st.section_versions.sections["s1"].source_bindings]
    distinct_quotes = [
        "基线时延为 18.4 ms[@ev1]。",
        "结论成立。",  # 5 chars, full Pandoc sentence -> allowed!
        "第三句补充复杂度分析内容。",
        "第四句讨论可复现性保障方案。",
    ]

    # 1) Short rationale 'ok' rejected
    with pytest.raises(ReviewChecklistIncompleteError):
        A(
            "submit_review",
            {
                "project_id": "p_r5",
                "task_id": t["task_id"],
                "section_revision": t["target_section_revision"],
                "context_fingerprint": t["target_context_fingerprint"],
                "findings": [],
                "review_summary": "short rationale",
                "recommendation": "approve",
                "reviewed_by": "reviewer-02",
                "checklist_hash": t["checklist_hash"],
                "claim_evaluations": ce,
                "checklist_results": [{"check_id": c["check_id"], "question": c["question"], "verdict": "pass", "rationale": "ok", "quotes": [distinct_quotes[i]]} for i, c in enumerate(t["checklist_items"])],
            },
        )

    # 2) 4-char partial quote '基线时延' rejected
    with pytest.raises(ReviewChecklistIncompleteError):
        A(
            "submit_review",
            {
                "project_id": "p_r5",
                "task_id": t["task_id"],
                "section_revision": t["target_section_revision"],
                "context_fingerprint": t["target_context_fingerprint"],
                "findings": [],
                "review_summary": "short partial quote",
                "recommendation": "approve",
                "reviewed_by": "reviewer-02",
                "checklist_hash": t["checklist_hash"],
                "claim_evaluations": ce,
                "checklist_results": [{"check_id": c["check_id"], "question": c["question"], "verdict": "pass", "rationale": "Substantive rationale exceeding fifteen chars.", "quotes": ["基线时延" if i == 0 else distinct_quotes[i]]} for i, c in enumerate(t["checklist_items"])],
            },
        )

    # 3) All 4 items quoting the same sentence rejected
    with pytest.raises(ReviewChecklistIncompleteError):
        A(
            "submit_review",
            {
                "project_id": "p_r5",
                "task_id": t["task_id"],
                "section_revision": t["target_section_revision"],
                "context_fingerprint": t["target_context_fingerprint"],
                "findings": [],
                "review_summary": "identical quotes",
                "recommendation": "approve",
                "reviewed_by": "reviewer-02",
                "checklist_hash": t["checklist_hash"],
                "claim_evaluations": ce,
                "checklist_results": [{"check_id": c["check_id"], "question": c["question"], "verdict": "pass", "rationale": "Substantive rationale exceeding fifteen chars.", "quotes": ["第三句补充复杂度分析内容。"]} for c in t["checklist_items"]],
            },
        )

    # 4) Valid distinct quotes including full short sentence '结论成立。' succeed
    ok_res = A(
        "submit_review",
        {
            "project_id": "p_r5",
            "task_id": t["task_id"],
            "section_revision": t["target_section_revision"],
            "context_fingerprint": t["target_context_fingerprint"],
            "findings": [],
            "review_summary": "Valid structured independent review.",
            "recommendation": "approve",
            "reviewed_by": "reviewer-02",
            "checklist_hash": t["checklist_hash"],
            "claim_evaluations": ce,
            "checklist_results": [{"check_id": c["check_id"], "question": c["question"], "verdict": "pass", "rationale": "Substantive rationale verified against text.", "quotes": [distinct_quotes[i]]} for i, c in enumerate(t["checklist_items"])],
        },
    )
    assert ok_res["status"] == "accepted"


def test_r6_default_profile_outline_review_and_unlisted_section_rejected(tmp_path: Path) -> None:
    """E8 regression: default profile + 2 outline nodes -> start_review creates 2 tasks;
    submitting a section outside approved outline raises SECTION_NOT_IN_OUTLINE."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "d.md").write_text("Baseline latency is 18.4 ms.\n", encoding="utf-8")
    ws = tmp_path / "ws"
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_e8", "title": "Default Outline Test"})
    ing = A("ingest_sources", {"project_id": "p_e8", "source_paths": [str(src_dir / "d.md")]})
    sid = ing["ingested_sources"][0]["source_id"]
    sl = A("list_source_blocks", {"project_id": "p_e8", "source_id": sid})["blocks"][0]
    A("register_evidence", {"project_id": "p_e8", "evidence_items": [{"evidence_id": "ev1", "source_id": sid, "slice_id": sl["slice_id"], "quote_excerpt": "Baseline latency is 18.4 ms"}]})
    A("submit_outline", {"project_id": "p_e8", "global_thesis": "t", "nodes": [{"section_id": "s1", "title": "S1"}, {"section_id": "s2", "title": "S2"}]})
    oreq = A("request_approval", {"project_id": "p_e8", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_e8", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")

    c1 = A("get_section_context", {"project_id": "p_e8", "section_id": "s1", "action": "draft"})
    with pytest.raises(ContractValidationError) as exc_unlisted:
        A(
            "submit_section",
            {
                "project_id": "p_e8",
                "section_id": "unlisted_sec",
                "base_revision": 0,
                "context_revision": c1["context_revision"],
                "context_fingerprint": c1["context_fingerprint"],
                "idempotency_key": "unlisted-1",
                "author_session": "writer-01",
                "content_markdown": "Text outside outline.",
                "summary": "Unlisted section",
            },
        )
    assert exc_unlisted.value.code == "SECTION_NOT_IN_OUTLINE"

    for s in ("s1", "s2"):
        c = A("get_section_context", {"project_id": "p_e8", "section_id": s, "action": "draft"})
        A("submit_section", {"project_id": "p_e8", "section_id": s, "base_revision": 0, "context_revision": c["context_revision"], "context_fingerprint": c["context_fingerprint"], "idempotency_key": f"k-{s}", "author_session": "writer-01", "content_markdown": "基线时延为 18.4 ms[@ev1]。说明完成。", "summary": f"Draft {s}"})

    rv = A("start_review", {"project_id": "p_e8", "reviewer_id": "reviewer-02"})
    assert len(rv["semantic_review_tasks"]) == 2
    assert len(rv["claimed_tasks"]) == 2


def test_r6_zero_deleted_aliases_and_no_legacy_tags_in_src() -> None:
    deleted_aliases = [
        "verifica" + "tion_status",
        "support_" + "rationale",
        "approv" + "ed_at",
        "approv" + "ed_by",
        "docx_" + "path",
        "output_" + "sha256",
        "last_rendered_" + "docx_path",
        "checklist_" + "questions",
        "docx_" + "sha256",
    ]
    alias_rx = re.compile(r"\b(" + "|".join(deleted_aliases) + r")\b")
    legacy_rx = re.compile(
        "|".join(
            [
                "legacy_" + "unverified",
                "legacy_budget_" + "origin_unknown",
                r"DAF-" + r"P\d",
                r"R2-" + r"P\d",
                r"R3-" + r"\d",
                r"T-" + r"R2-",
            ]
        )
    )

    assert "support_status" not in SourceBinding.model_fields
    assert "target_section_ids" not in SemanticReviewTask.model_fields
    assert not (SRC_DAF / "document_engine" / "ir.py").exists()

    for p in sorted(SRC_DAF.rglob("*.py")):
        txt = p.read_text(encoding="utf-8")
        assert not alias_rx.search(txt), f"Deleted alias found in {p}"
        assert not legacy_rx.search(txt), f"Legacy/remediation tag found in {p}"


def test_r8_no_direct_store_transactions_outside_unit_tests() -> None:
    forbidden = "transact_" + "project"
    unit_store = (TESTS_DIR / "unit" / "test_store.py").resolve()
    violations: list[str] = []
    for p in sorted(TESTS_DIR.rglob("*.py")):
        if p.resolve() == unit_store:
            continue
        if forbidden in p.read_text(encoding="utf-8"):
            violations.append(str(p.relative_to(REPO_ROOT)))
    assert violations == [], f"Acceptance tests must not call {forbidden}: {violations}"


def test_r6_version_consistency_across_package_http_ui_registry_and_skill(tmp_path: Path) -> None:
    pyproject_data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkg_version = pyproject_data["project"]["version"]
    assert daf.__version__ == pkg_version
    ws = tmp_path / "ws"
    http_app = create_http_app(workspace_root=ws)
    ui_app, _ = create_local_ui_app(workspace_root=ws)
    assert http_app.version == pkg_version
    assert ui_app.version == pkg_version

    eng = DAFEngine(workspace_root=ws)
    caps = eng.get_capabilities()
    assert PROTOCOL_VERSION == "2.0"
    assert caps["protocol_version"] == PROTOCOL_VERSION
    assert caps["environment"]["supported_template_modes"] == ["builtin", "mapped"]
    skill_text = (REPO_ROOT / "agent_skill" / "SKILL.md").read_text(encoding="utf-8")
    assert f"v{PROTOCOL_VERSION}" in skill_text


def test_r7a_1500_slices_state_json_under_50kb_and_write_fast(tmp_path: Path) -> None:
    """E1 regression: 1500-slice corpus yields compact state_json (< 50 KB) and O(1) warm writes."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    para = "本研究提出一种轻量化的推理方法，在基准数据集上时延降低到 9.2 ms。" * 20 + "\n\n"
    corpus = src_dir / "big.md"
    corpus.write_text("# 资料\n\n" + para * 1500, encoding="utf-8")

    ws = tmp_path / "ws"
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_perf", "title": "1500 Slice Scale Test"})
    ing = A("ingest_sources", {"project_id": "p_perf", "source_paths": [str(corpus)]})
    assert ing["total_slices"] >= 1500

    timings_ms: list[float] = []
    for i in range(10):
        t0 = time.perf_counter()
        A("record_agent_note", {"project_id": "p_perf", "note_text": f"note {i}"})
        timings_ms.append((time.perf_counter() - t0) * 1000.0)
    median_ms = sorted(timings_ms)[len(timings_ms) // 2]

    with closing(sqlite3.connect(ws / "daf_workspace.sqlite3")) as conn:
        state_len = conn.execute("SELECT length(state_json) FROM projects WHERE project_id = 'p_perf'").fetchone()[0]
    assert state_len < 50_000, f"state_json too large: {state_len}"
    assert median_ms < 50.0, f"Warm note write too slow at 1500 slices: median={median_ms:.2f} ms"


def test_http_api_error_mappings_and_local_ui_bibliography_routes(tmp_path: Path) -> None:
    """Experiment D regression: verify FastAPI HTTP 403/409/500 mappings & Local UI bibliography portal."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src_md = src_dir / "s.md"
    src_md.write_text("Throughput is 3.2 GB/s.\n", encoding="utf-8")
    ws = tmp_path / "ws"
    http_client = TestClient(create_http_app(workspace_root=ws, allowed_source_roots=[src_dir]))

    assert http_client.post("/api/v1/tools/create_project", json={"params": {"project_id": "h1", "title": "HTTP"}}).status_code == 200
    ing = http_client.post("/api/v1/tools/ingest_sources", json={"params": {"project_id": "h1", "source_paths": [str(src_md)]}}).json()["data"]
    sid = ing["ingested_sources"][0]["source_id"]

    # Agent blocked from calling user-only bibliography route via HTTP (403)
    resp_403 = http_client.post("/api/v1/tools/set_source_bibliography_by_user", json={"params": {"project_id": "h1", "source_id": sid, "csl_json": {}}})
    assert resp_403.status_code == 403

    # Setup single section + approval + missing bib -> HTTP 409 BIBLIOGRAPHY_MISSING on release render
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    sl = eng.list_source_blocks("h1", source_id=sid)["blocks"][0]
    eng.call_agent_tool("register_evidence", {"project_id": "h1", "evidence_items": [{"evidence_id": "ev1", "source_id": sid, "slice_id": sl["slice_id"], "quote_excerpt": "Throughput is 3.2 GB/s"}]})
    eng.call_agent_tool("submit_outline", {"project_id": "h1", "global_thesis": "t", "nodes": [{"section_id": "s1", "title": "S1", "target_min_words": 5}]})
    oreq = eng.call_agent_tool("request_approval", {"project_id": "h1", "artifact_type": "outline"})
    eng.decide_approval_by_human("h1", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")
    ctx = eng.call_agent_tool("get_section_context", {"project_id": "h1", "section_id": "s1", "action": "draft"})
    eng.call_agent_tool("submit_section", {"project_id": "h1", "section_id": "s1", "base_revision": 0, "context_revision": ctx["context_revision"], "context_fingerprint": ctx["context_fingerprint"], "idempotency_key": "hs1", "author_session": "writer-01", "content_markdown": "实测吞吐为 3.2 GB/s[@ev1]。说明完成。", "summary": "S1"})
    rv = eng.call_agent_tool("start_review", {"project_id": "h1", "reviewer_id": "reviewer-02"})
    t = rv["claimed_tasks"][0]
    st = eng.store.load_state("h1")
    bindings = st.section_versions.sections["s1"].source_bindings
    eng.call_agent_tool("submit_review", {"project_id": "h1", "task_id": t["task_id"], "section_revision": t["target_section_revision"], "context_fingerprint": t["target_context_fingerprint"], "findings": [], "review_summary": "OK", "recommendation": "approve", "reviewed_by": "reviewer-02", "claim_evaluations": [{"binding_id": bindings[0].binding_id, "semantic_support_status": "supported", "rationale": "Verified numeric throughput against excerpt."}], "checklist_results": [{"check_id": c["check_id"], "question": c["question"], "verdict": "pass", "rationale": "Throughput properly grounded in source evidence.", "quotes": ["实测吞吐为 3.2 GB/s[@ev1]。"]} for c in t["checklist_items"]], "checklist_hash": t["checklist_hash"]})
    freq = eng.call_agent_tool("request_approval", {"project_id": "h1", "artifact_type": "final_draft"})
    eng.decide_approval_by_human("h1", freq["approval_request_id"], "approved", confirmation_channel="local_cli")

    resp_409 = http_client.post("/api/v1/tools/render_document", json={"params": {"project_id": "h1", "purpose": "release"}})
    assert resp_409.status_code == 409
    assert resp_409.json()["error"]["code"] == "BIBLIOGRAPHY_MISSING"

    # Local UI bibliography form + token auth
    ui_app, tok = create_local_ui_app(workspace_root=ws, allowed_source_roots=[src_dir])
    ui_client = TestClient(ui_app)
    csl_payload = json.dumps({"type": "article-journal", "title": "HTTP UI Confirmed Title", "author": [{"family": "Liu"}], "issued": {"date-parts": [[2025]]}})
    assert ui_client.post(f"/projects/h1/sources/{sid}/bibliography", data={"csl_json": csl_payload}).status_code == 403
    ok_ui = ui_client.post(f"/projects/h1/sources/{sid}/bibliography?token={tok}", data={"csl_json": csl_payload})
    assert ok_ui.status_code == 200
    assert http_client.post("/api/v1/tools/render_document", json={"params": {"project_id": "h1", "purpose": "release"}}).status_code == 200
