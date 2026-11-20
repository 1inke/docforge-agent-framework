"""Round-3 regression suite covering grouped/clause-scoped citations, R-B unit checks,
review hardening (case-insensitive reviewer & distinct rationales), full release + table caption warning.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from daf import DAFEngine
from daf.core.errors import (
    BibliographyMissingError,
    ContractValidationError,
    ReviewChecklistIncompleteError,
)
from daf.core.pandoc_ast import analyze_markdown_with_pandoc
from tests.helpers import assert_clean_rendered_docx


def _setup_two_evidence_project(
    tmp_path: Path,
    project_id: str,
    *,
    target_min_words: int | None = 5,
) -> tuple[DAFEngine, Path, str]:
    src_dir = tmp_path / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    src_file = src_dir / "bench.md"
    src_file.write_text(
        "# Benchmark\n\nThe baseline latency is 18.4 ms. The new method reaches 9.2 ms on the same benchmark.\n",
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": project_id, "title": "Round-3 Test Project"})
    ing = A("ingest_sources", {"project_id": project_id, "source_paths": [str(src_file)]})
    sid = ing["ingested_sources"][0]["source_id"]
    sl_id = A("find_sources", {"project_id": project_id, "query": "18.4 ms", "source_ids": [sid]})["matches"][0]["slice_id"]
    A(
        "register_evidence",
        {
            "project_id": project_id,
            "evidence_items": [
                {
                    "evidence_id": "e1",
                    "source_id": sid,
                    "slice_id": sl_id,
                    "quote_excerpt": "The baseline latency is 18.4 ms.",
                },
                {
                    "evidence_id": "e2",
                    "source_id": sid,
                    "slice_id": sl_id,
                    "quote_excerpt": "The new method reaches 9.2 ms",
                },
            ],
        },
    )
    node = {"section_id": "s1", "title": "1. Evaluation", "assigned_evidence_ids": ["e1", "e2"]}
    if target_min_words is not None:
        node["target_min_words"] = target_min_words
    A("submit_outline", {"project_id": project_id, "global_thesis": "Evaluation thesis", "nodes": [node]})
    oreq = A("request_approval", {"project_id": project_id, "artifact_type": "outline"})
    eng.decide_approval_by_human(project_id, oreq["approval_request_id"], "approved", confirmation_channel="local_cli")
    return eng, ws, sid


def _submit(eng: DAFEngine, project_id: str, section_id: str, md: str, *, author: str = "writer_session", key: str = "k") -> dict:
    st = eng.store.load_state(project_id)
    rev = st.section_versions.sections[section_id].revision if section_id in st.section_versions.sections else 0
    ctx = eng.call_agent_tool("get_section_context", {"project_id": project_id, "section_id": section_id})
    return eng.call_agent_tool(
        "submit_section",
        {
            "project_id": project_id,
            "section_id": section_id,
            "base_revision": rev,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": f"{key}-{rev}",
            "author_session": author,
            "content_markdown": md,
            "summary": "Round-3 test submission",
        },
    )


def test_round3_e12_grouped_citations_parsed_and_nonexistent_blocked(tmp_path: Path) -> None:
    """E12 regression: [@e1; @e2] is not split at ';' during AST sentence splitting;
    nonexistent grouped evidence rejects submission with EVIDENCE_UNVERIFIED (including with '将')."""
    s_single, _, _ = analyze_markdown_with_pandoc("时延为 18.4 ms[@e1]。")
    assert [s.citation_ids for s in s_single] == [["e1"]]

    s_grp, stats_grp, _ = analyze_markdown_with_pandoc("时延从 18.4 ms 降到 9.2 ms[@e1; @e2]。")
    assert [s.citation_ids for s in s_grp] == [["e1", "e2"]]
    assert stats_grp.citations == ["e1", "e2"]

    s_nope, stats_nope, _ = analyze_markdown_with_pandoc("时延从 18.4 ms 降到 9.2 ms[@nope1; @nope2]。")
    assert [s.citation_ids for s in s_nope] == [["nope1", "nope2"]]
    assert stats_nope.citations == ["nope1", "nope2"]

    s_loc, _, _ = analyze_markdown_with_pandoc("时延为 18.4 ms [@e1, p. 3]。")
    assert [s.citation_ids for s in s_loc] == [["e1"]]

    eng, _, _ = _setup_two_evidence_project(tmp_path, "p_e12")

    for bad_md, tag in (
        ("新方法时延为 99.9 ms[@nope1; @nope2]。", "bad1"),
        ("该方法将时延从 18.4 ms 降到 9.2 ms[@nope1; @nope2]。", "bad2"),
    ):
        with pytest.raises(ContractValidationError) as exc_info:
            _submit(eng, "p_e12", "s1", bad_md, key=tag)
        assert exc_info.value.code == "EVIDENCE_UNVERIFIED"

    res_dup = _submit(eng, "p_e12", "s1", "新方法时延为 99.9 ms[@e1; @e1]。", key="dup")
    assert len(res_dup["source_bindings"]) == 1
    assert res_dup["source_bindings"][0]["rule_check_status"] == "rule_failed"
    assert len(res_dup["l2_claim_support_warnings"]) == 1


def test_round3_e11_rb_clause_scoping_grouped_union_and_unit_mismatch(tmp_path: Path) -> None:
    """E11 regression: separate clauses scope to each citation; grouped citation checks union;
    swapped citations, unit mismatches (s vs ms), and rounding (18 vs 18.4) flag rule_failed."""
    eng, _, sid = _setup_two_evidence_project(tmp_path, "p_e11")
    A = eng.call_agent_tool

    # 1) Same-sentence separate clauses: 0 warnings
    r_sep = _submit(eng, "p_e11", "s1", "基线时延为 18.4 ms[@e1]，新方法为 9.2 ms[@e2]。", key="sep")
    assert len(r_sep["source_bindings"]) == 2
    assert r_sep["l2_claim_support_warnings"] == []

    # 2) Swapped citations: both clauses fail
    r_swap = _submit(eng, "p_e11", "s1", "基线时延为 18.4 ms[@e2]，新方法为 9.2 ms[@e1]。", key="swap")
    assert [(w["evidence_id"], w["rule_check_status"]) for w in r_swap["l2_claim_support_warnings"]] == [
        ("e2", "rule_failed"),
        ("e1", "rule_failed"),
    ]

    # 3) Unit mismatch: 18.4 s vs 18.4 ms -> rule_failed
    r_unit = _submit(eng, "p_e11", "s1", "基线时延为 18.4 s[@e1]。", key="unit")
    assert len(r_unit["l2_claim_support_warnings"]) == 1
    assert r_unit["l2_claim_support_warnings"][0]["rule_check_status"] == "rule_failed"
    assert "unit" in r_unit["l2_claim_support_warnings"][0]["rationale"].lower()

    # 4) Rounding: 18 ms vs 18.4 ms -> rule_failed
    r_round = _submit(eng, "p_e11", "s1", "基线时延约为 18 ms[@e1]。", key="round")
    assert [(w["evidence_id"], w["rule_check_status"]) for w in r_round["l2_claim_support_warnings"]] == [
        ("e1", "rule_failed")
    ]

    # 5) Grouped citation [@e1; @e2]: checks union and completes end-to-end release
    r_grp = _submit(eng, "p_e11", "s1", "新方法将时延从 18.4 ms 降到 9.2 ms[@e1; @e2]。", key="grp")
    assert len(r_grp["source_bindings"]) == 2
    assert r_grp["l2_claim_support_warnings"] == []

    eng.set_source_bibliography_by_user(
        "p_e11",
        sid,
        {"id": "x", "type": "article-journal", "title": "Grouped Citation Benchmark", "author": [{"family": "Li"}], "issued": {"date-parts": [[2025]]}},
    )
    rv = A("start_review", {"project_id": "p_e11", "reviewer_id": "reviewer_indep"})
    assert rv["open_issue_counts"]["blocker"] == 0
    task = rv["claimed_tasks"][0]
    st = eng.store.load_state("p_e11")
    bindings = st.section_versions.sections["s1"].source_bindings
    sub_rev = A(
        "submit_review",
        {
            "project_id": "p_e11",
            "task_id": task["task_id"],
            "section_revision": task["target_section_revision"],
            "context_fingerprint": task["target_context_fingerprint"],
            "findings": [],
            "review_summary": "Verified grouped co-citation union against both excerpts.",
            "recommendation": "approve",
            "reviewed_by": "reviewer_indep",
            "checklist_hash": task["checklist_hash"],
            "claim_evaluations": [
                {"binding_id": b.binding_id, "semantic_support_status": "supported", "rationale": f"Verified binding {idx} against co-cited excerpts."}
                for idx, b in enumerate(bindings)
            ],
            "checklist_results": [
                {
                    "check_id": item["check_id"],
                    "question": item["question"],
                    "verdict": "pass",
                    "rationale": f"Checklist item {idx} ({item['question'][:18]}) verified with grouped citation.",
                    "quotes": ["新方法将时延从 18.4 ms 降到 9.2 ms[@e1; @e2]。"],
                }
                for idx, item in enumerate(task["checklist_items"])
            ],
        },
    )
    assert sub_rev["status"] == "accepted"
    freq = A("request_approval", {"project_id": "p_e11", "artifact_type": "final_draft"})
    eng.decide_approval_by_human("p_e11", freq["approval_request_id"], "approved", confirmation_channel="local_cli")
    rel = A("render_document", {"project_id": "p_e11", "purpose": "release"})
    rel_txt = assert_clean_rendered_docx(Path(rel["artifact_path"]), expected_bib_count=1)
    assert "18.4 ms" in rel_txt and "9.2 ms" in rel_txt


def test_round3_e2d_case_insensitive_reviewer_and_distinct_rationales(tmp_path: Path) -> None:
    """E2d regression: case-insensitive REVIEWER_IS_AUTHOR blocks Writer_session vs writer_session,
    and copy-pasting one identical rationale across multi-item checklists is rejected."""
    eng, _, _ = _setup_two_evidence_project(tmp_path, "p_e2d")
    A = eng.call_agent_tool
    body = "本节说明研究背景与意义。我们关注密集人群场景下的轨迹预测问题。"
    _submit(eng, "p_e2d", "s1", body, author="writer_session", key="e2d")

    # Case F: case variant of author_session rejected immediately
    with pytest.raises(ContractValidationError) as exc_case:
        A("start_review", {"project_id": "p_e2d", "reviewer_id": "Writer_session"})
    assert exc_case.value.code == "REVIEWER_IS_AUTHOR"

    rv = A("start_review", {"project_id": "p_e2d", "reviewer_id": "reviewer_session"})
    task = rv["claimed_tasks"][0]
    assert len(task["checklist_items"]) >= 2

    # Case C: identical rationale copy-pasted across all active items is rejected
    with pytest.raises(ReviewChecklistIncompleteError):
        A(
            "submit_review",
            {
                "project_id": "p_e2d",
                "task_id": task["task_id"],
                "section_revision": task["target_section_revision"],
                "context_fingerprint": task["target_context_fingerprint"],
                "findings": [],
                "review_summary": "All items checked.",
                "recommendation": "approve",
                "reviewed_by": "reviewer_session",
                "checklist_hash": task["checklist_hash"],
                "claim_evaluations": [],
                "checklist_results": [
                    {
                        "check_id": item["check_id"],
                        "question": item["question"],
                        "verdict": "pass",
                        "rationale": "经检查本项完全符合要求，没有发现任何问题。",
                        "quotes": ["本节说明研究背景与意义。"],
                    }
                    for item in task["checklist_items"]
                ],
            },
        )

    # Refresh rejected task via start_review and submit distinct per-item rationales
    rv2 = A("start_review", {"project_id": "p_e2d", "reviewer_id": "reviewer_session"})
    task2 = rv2["claimed_tasks"][0]
    ok = A(
        "submit_review",
        {
            "project_id": "p_e2d",
            "task_id": task2["task_id"],
            "section_revision": task2["target_section_revision"],
            "context_fingerprint": task2["target_context_fingerprint"],
            "findings": [],
            "review_summary": "Distinct checklist rationales verified.",
            "recommendation": "approve",
            "reviewed_by": "reviewer_session",
            "checklist_hash": task2["checklist_hash"],
            "claim_evaluations": [],
            "checklist_results": [
                {
                    "check_id": item["check_id"],
                    "question": item["question"],
                    "verdict": "pass",
                    "rationale": f"核查条目 {idx}（{item['question'][:14]}）已在正文阐明对应依据。",
                    "quotes": ["本节说明研究背景与意义。"],
                }
                for idx, item in enumerate(task2["checklist_items"])
            ],
        },
    )
    assert ok["status"] == "accepted"


def test_round3_e9_e9b_full_release_and_table_caption_syntax_warning(tmp_path: Path) -> None:
    """E9 & E9b regression: caption attribute syntax ': Caption {#tbl:lat}' warns with non-blocking
    minor issue on submit_section while still rendering cleanly alongside OMML & GB/T 7714."""
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    src_file = src_dir / "d.md"
    src_file.write_text(
        "# D\n\nThe baseline latency is 18.4 ms. The new method reaches 9.2 ms on the same benchmark.\n",
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_e9", "title": "测试申报书"})
    ing = A("ingest_sources", {"project_id": "p_e9", "source_paths": [str(src_file)]})
    sid = ing["ingested_sources"][0]["source_id"]
    sl_id = A("find_sources", {"project_id": "p_e9", "query": "18.4 ms", "source_ids": [sid]})["matches"][0]["slice_id"]
    A(
        "register_evidence",
        {
            "project_id": "p_e9",
            "evidence_items": [
                {"evidence_id": "e1", "source_id": sid, "slice_id": sl_id, "quote_excerpt": "The baseline latency is 18.4 ms."},
                {"evidence_id": "e2", "source_id": sid, "slice_id": sl_id, "quote_excerpt": "The new method reaches 9.2 ms"},
            ],
        },
    )
    A(
        "submit_outline",
        {
            "project_id": "p_e9",
            "global_thesis": "x",
            "nodes": [
                {"section_id": "s1", "title": "研究基础"},
                {"section_id": "s2", "title": "研究方案"},
            ],
        },
    )
    oreq = A("request_approval", {"project_id": "p_e9", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_e9", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")

    s1_md = (
        "如表 @tbl:lat 所示，基线时延为 18.4 ms[@e1]。新方法为 9.2 ms[@e2]。\n\n"
        "| 方法 | 时延 |\n|---|---|\n| 基线 | 18.4 ms |\n| 新方法 | 9.2 ms |\n\n"
        ": 时延对比 {#tbl:lat}\n\n"
        "本节进一步说明研究的动机与整体思路。密集人群场景中行人之间的交互复杂多变，传统方法难以兼顾精度与效率。"
        "我们希望在保持模型轻量的同时提升预测的稳定性，并为后续的工程部署提供清晰的路线。"
        "围绕这一目标，本节梳理了问题背景、现有方法的不足以及本项目拟采用的技术路线，为后续章节的展开奠定基础。\n"
    )
    s2_md = (
        "本项目计划 3 年完成。我们预期该方法可推广到密集场景[@hypothesis]。损失函数见式 @eq:loss，结果回顾见表 @tbl:lat。\n\n"
        "[$$ L = \\sum_i \\lVert \\hat{y}_i - y_i \\rVert^2 $$]{#eq:loss}\n\n"
        "本节进一步说明研究的动机与整体思路。密集人群场景中行人之间的交互复杂多变，传统方法难以兼顾精度与效率。"
        "我们希望在保持模型轻量的同时提升预测的稳定性，并为后续的工程部署提供清晰的路线。"
        "围绕这一目标，本节梳理了问题背景、现有方法的不足以及本项目拟采用的技术路线，为后续章节的展开奠定基础。\n"
    )

    r1 = _submit(eng, "p_e9", "s1", s1_md, key="e9-s1")
    assert r1["syntax_warnings"], "Expected TABLE_CAPTION_ATTR_SYNTAX warning on s1"
    assert r1["syntax_warnings"][0]["code"] == "TABLE_CAPTION_ATTR_SYNTAX"
    assert r1["syntax_warnings"][0]["label"] == "tbl:lat"

    r2 = _submit(eng, "p_e9", "s2", s2_md, key="e9-s2")
    assert r2["syntax_warnings"] == []

    rv = A("start_review", {"project_id": "p_e9", "reviewer_id": "reviewer_session"})
    assert rv["open_issue_counts"]["blocker"] == 0
    assert rv["open_issue_counts"]["major"] == 0
    assert rv["open_issue_counts"]["minor"] >= 1

    st = eng.store.load_state("p_e9")
    assert any(
        i.category == "format_equation" and i.severity == "minor" and i.target_section_id == "s1" and "tbl:lat" in i.description
        for i in st.review_ledger.issues
    )

    for task in rv["claimed_tasks"]:
        sec = st.section_versions.sections[task["section_id"]]
        quote = [s for s in sec.content_markdown.replace("\n", " ").split("。") if len(s.strip()) > 12][0].strip() + "。"
        sub = A(
            "submit_review",
            {
                "project_id": "p_e9",
                "task_id": task["task_id"],
                "section_revision": task["target_section_revision"],
                "context_fingerprint": task["target_context_fingerprint"],
                "findings": [],
                "review_summary": "已逐项核对正文表述、公式与交叉引用。",
                "recommendation": "approve",
                "checklist_hash": task["checklist_hash"],
                "reviewed_by": "reviewer_session",
                "claim_evaluations": [
                    {
                        "binding_id": b.binding_id,
                        "semantic_support_status": "hypothesis_proposal" if b.claim_type == "research_hypothesis" else "supported",
                        "rationale": f"核查绑定 {b.binding_id} 逻辑与数值表述一致。",
                    }
                    for b in sec.source_bindings
                ],
                "checklist_results": [
                    {
                        "check_id": item["check_id"],
                        "question": item["question"],
                        "verdict": "pass",
                        "rationale": f"核对第 {idx} 项（{item['question'][:14]}）在正文中具有清晰陈述。",
                        "quotes": [quote],
                    }
                    for idx, item in enumerate(task["checklist_items"])
                ],
            },
        )
        assert sub["status"] == "accepted"

    freq = A("request_approval", {"project_id": "p_e9", "artifact_type": "final_draft"})
    eng.decide_approval_by_human("p_e9", freq["approval_request_id"], "approved", confirmation_channel="local_cli")

    with pytest.raises(BibliographyMissingError) as exc_bib:
        A("render_document", {"project_id": "p_e9", "purpose": "release"})
    assert exc_bib.value.code == "BIBLIOGRAPHY_MISSING"

    eng.set_source_bibliography_by_user(
        "p_e9",
        sid,
        {
            "id": "x",
            "type": "article-journal",
            "title": "轻量化行人轨迹预测方法",
            "author": [{"family": "张", "given": "三"}],
            "container-title": "测试学报",
            "issued": {"date-parts": [[2024]]},
            "volume": "1",
            "issue": "2",
            "page": "1-10",
        },
        updated_by="me",
    )

    out = A("render_document", {"project_id": "p_e9", "purpose": "release"})
    docx_path = Path(out["artifact_path"])
    out_txt = assert_clean_rendered_docx(docx_path, expected_bib_count=1)
    assert "表 1 时延对比" in out_txt
    assert "式 (1)" in out_txt
    assert "轻量化行人轨迹预测方法" in out_txt
    with zipfile.ZipFile(docx_path) as zf:
        assert "<m:oMath" in zf.read("word/document.xml").decode("utf-8")

    exp = A("export_document", {"project_id": "p_e9", "purpose": "release"})
    assert exp["is_official_release"] is True
    assert Path(exp["artifact_path"]).exists()
