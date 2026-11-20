"""Round-2 R3 narrowed R-A uncited factual claim rules & major vs minor severity tests."""

from __future__ import annotations

from pathlib import Path

from daf import DAFEngine
from daf.core.pandoc_ast import classify_uncited_sentence, sentence_needs_citation
from tests.helpers import assert_clean_rendered_docx

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "adversarial"


def test_r3_adversarial_negatives_zero_false_positives() -> None:
    lines = [
        line.strip()
        for line in (FIXTURES_DIR / "ra_negatives.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    false_positives = [s for s in lines if sentence_needs_citation(s)]
    assert false_positives == [], f"Unexpected R-A false positives: {false_positives}"


def test_r3_adversarial_positives_major_and_minor() -> None:
    lines = [
        line.strip()
        for line in (FIXTURES_DIR / "ra_positives.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for line in lines:
        expected_sev, sentence = line.split("|", 1)
        got = classify_uncited_sentence(sentence)
        assert got == expected_sev, f"Sentence {sentence!r}: expected {expected_sev}, got {got}"

    # Planning vs completed-state override
    assert classify_uncited_sentence("拟将时延降低到 5 ms。") is None
    assert classify_uncited_sentence("实验表明时延降低到 5 ms。") == "major"


def test_r3_extra_physical_units_and_minor_does_not_block_release(tmp_path: Path) -> None:
    assert classify_uncited_sentence("发射功率设为 23 dBm。") is None
    assert classify_uncited_sentence("发射功率设为 23 dBm。", extra_units=["dBm"]) == "major"

    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    (src_dir / "ref.md").write_text("Throughput reached 3.2 GB/s.\n", encoding="utf-8")
    ws = tmp_path / "ws"
    prof_dir = ws / "profiles"
    prof_dir.mkdir(parents=True)
    (prof_dir / "r3_prof.yaml").write_text(
        """profile_id: r3_prof
display_name: R3 Extra Units Profile
sections:
  - id: s1
    title: 1. Evaluation
    required: true
    words: [5, 2000]
    review_checklist:
      - 实验指标是否有对应证据支撑？
content:
  extra_physical_units: [dBm]
""",
        encoding="utf-8",
    )

    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_r3", "title": "R3 Minor vs Major", "profile_id": "r3_prof"})
    ing = A("ingest_sources", {"project_id": "p_r3", "source_paths": [str(src_dir / "ref.md")]})
    sid = ing["ingested_sources"][0]["source_id"]
    sl = A("list_source_blocks", {"project_id": "p_r3", "source_id": sid})["blocks"][0]
    A("register_evidence", {"project_id": "p_r3", "evidence_items": [{"evidence_id": "ev_1", "source_id": sid, "slice_id": sl["slice_id"], "quote_excerpt": "Throughput reached 3.2 GB/s"}]})
    eng.set_source_bibliography_by_user("p_r3", sid, {"type": "article-journal", "title": "RF Throughput Study", "author": [{"family": "Chen"}], "issued": {"date-parts": [[2025]]}})
    A("submit_outline", {"project_id": "p_r3", "global_thesis": "t", "nodes": [{"section_id": "s1", "title": "1. Evaluation", "assigned_evidence_ids": ["ev_1"]}]})
    oreq = A("request_approval", {"project_id": "p_r3", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_r3", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")

    # 1) Custom extra unit 'dBm' triggers uncited major; comparative '优于' triggers minor
    ctx = A("get_section_context", {"project_id": "p_r3", "section_id": "s1", "action": "draft"})
    sub_bad = A(
        "submit_section",
        {
            "project_id": "p_r3",
            "section_id": "s1",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "r3-k1",
            "author_session": "writer-1",
            "content_markdown": "实测吞吐达到 3.2 GB/s[@ev_1]。本方法优于现有方法。发射功率设为 23 dBm。",
            "summary": "Check major + minor uncited",
        },
    )
    sevs = {i["severity"] for i in sub_bad["uncited_claim_issues"]}
    assert "major" in sevs and "minor" in sevs
    rv_bad = A("start_review", {"project_id": "p_r3", "reviewer_id": "reviewer-2"})
    assert rv_bad["open_issue_counts"]["major"] >= 1
    assert rv_bad["open_issue_counts"]["minor"] >= 1

    # 2) Removing the uncited '23 dBm' leaves ONLY minor issue, which must NOT block release
    ctx2 = A("get_section_context", {"project_id": "p_r3", "section_id": "s1", "action": "revise"})
    sub_ok = A(
        "submit_section",
        {
            "project_id": "p_r3",
            "section_id": "s1",
            "base_revision": 1,
            "context_revision": ctx2["context_revision"],
            "context_fingerprint": ctx2["context_fingerprint"],
            "idempotency_key": "r3-k2",
            "author_session": "writer-1",
            "content_markdown": "实测吞吐达到 3.2 GB/s[@ev_1]。本方法优于现有方法。",
            "summary": "Minor only",
        },
    )
    assert [i["severity"] for i in sub_ok["uncited_claim_issues"]] == ["minor"]

    rv = A("start_review", {"project_id": "p_r3", "reviewer_id": "reviewer-2"})
    assert rv["open_issue_counts"]["blocker"] == 0
    assert rv["open_issue_counts"]["major"] == 0
    assert rv["open_issue_counts"]["minor"] >= 1
    t = rv["claimed_tasks"][0]
    st = eng.store.load_state("p_r3")
    bindings = st.section_versions.sections["s1"].source_bindings
    A(
        "submit_review",
        {
            "project_id": "p_r3",
            "task_id": t["task_id"],
            "section_revision": t["target_section_revision"],
            "context_fingerprint": t["target_context_fingerprint"],
            "findings": [],
            "review_summary": "Verified measurement excerpt.",
            "recommendation": "approve",
            "reviewed_by": "reviewer-2",
            "claim_evaluations": [{"binding_id": bindings[0].binding_id, "semantic_support_status": "supported", "rationale": "Exact number match verified against slice."}],
            "checklist_results": [{"check_id": t["checklist_items"][0]["check_id"], "question": t["checklist_items"][0]["question"], "verdict": "pass", "rationale": "Throughput metric properly grounded in cited excerpt.", "quotes": ["实测吞吐达到 3.2 GB/s[@ev_1]。"], "evidence_ids": ["ev_1"]}],
            "checklist_hash": t["checklist_hash"],
        },
    )
    freq = A("request_approval", {"project_id": "p_r3", "artifact_type": "final_draft"})
    eng.decide_approval_by_human("p_r3", freq["approval_request_id"], "approved", confirmation_channel="local_cli")
    rel = A("render_document", {"project_id": "p_r3", "purpose": "release"})
    assert_clean_rendered_docx(Path(rel["artifact_path"]), expected_bib_count=1)
