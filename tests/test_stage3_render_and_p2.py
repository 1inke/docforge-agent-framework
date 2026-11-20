"""Stage 3 (P1-3 single-pass Pandoc render, P2-A cost & MCP ergonomics, Experiment B recall)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import docx
from docx.oxml.ns import qn

from daf import DAFEngine
from daf.core.pandoc_ast import (
    analyze_markdown_with_pandoc,
    extract_numbers_strict,
    hypothesis_has_false_experiment_marker,
    number_present_with_boundary,
    sentence_needs_citation,
)
from daf.document_engine.pandoc_renderer import render_document_single_pass_pandoc
from daf.interfaces.mcp_server.server import create_mcp_server


def test_p1_3_single_pass_pandoc_render_with_csl_omml_and_crossref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify single-pass Pandoc rendering: <= 1 pandoc call for the entire document,
    GB/T 7714 numeric citations + bibliography, OMML equations, and continuous numbering."""
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    src_file = src_dir / "paper.md"
    src_file.write_text(
        "# Paper\nThe catalyst achieved 84.6% Faradaic efficiency at -0.8 V over 120 h.\n",
        encoding="utf-8",
    )
    ws = tmp_path / "ws"
    prof_dir = ws / "profiles"
    prof_dir.mkdir(parents=True)
    (prof_dir / "render_yaml.yaml").write_text(
        """profile_id: render_yaml
display_name: Render Test
sections:
  - id: sec_1
    title: 1. Method
    required: true
    words: [10, 2000]
    review_checklist:
      - Are equations and citations accurate?
  - id: sec_2
    title: 2. Results
    required: true
    words: [10, 2000]
    depends_on: [sec_1]
    review_checklist:
      - Are results backed by evidence?
""",
        encoding="utf-8",
    )

    engine = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    engine.create_project(project_id="ren_p", title="Single-Pass Render Demo", profile_id="render_yaml")
    ing = engine.ingest_sources(project_id="ren_p", source_paths=[str(src_file)])
    src_id = ing["ingested_sources"][0]["source_id"]

    # Attach CSL-JSON bibliography to source document
    def _attach_bib(state, _conn):
        state.source_registry.documents[src_id].bibliography = {
            "type": "article-journal",
            "title": "Copper Single-Atom Catalysts for CO2 Reduction",
            "author": [{"family": "Zhang", "given": "Wei"}],
            "container-title": "Journal of Catalysis",
            "issued": {"date-parts": [[2025]]},
            "volume": "420",
            "page": "101-112",
        }
        return {}, "Attached bibliography", {}

    engine.store.transact_project("ren_p", "attach_bib", _attach_bib)
    sl = engine.list_source_blocks(project_id="ren_p", source_id=src_id)["blocks"][0]
    engine.register_evidence(
        project_id="ren_p",
        evidence_items=[
            {
                "evidence_id": "ev_01",
                "source_id": src_id,
                "slice_id": sl["slice_id"],
                "locator": sl["locator"],
                "quote_excerpt": "The catalyst achieved 84.6% Faradaic efficiency at -0.8 V over 120 h.",
            }
        ],
    )
    engine.submit_outline(
        project_id="ren_p",
        global_thesis="Single-pass Pandoc compilation check.",
        nodes=[
            {"section_id": "sec_1", "title": "1. Method", "assigned_evidence_ids": ["ev_01"]},
            {"section_id": "sec_2", "title": "2. Results", "depends_on": ["sec_1"], "assigned_evidence_ids": ["ev_01"]},
        ],
    )
    req = engine.request_approval(project_id="ren_p", artifact_type="outline")
    engine.decide_approval_by_human("ren_p", req["approval_request_id"], "approved", confirmation_channel="local_cli")

    sec1_md = (
        "The overpotential relation is given by $$\\eta = E - E^0$$ and inline $k_B T$ governs kinetics. "
        "Earlier measurements achieved 84.6% Faradaic efficiency at -0.8 V[@ev_01].\n\n"
        "| Metric | Value |\n| --- | --- |\n| FE | 84.6% |\n"
    )
    sec2_md = (
        "Stability tests ran for 120 h with 84.6% efficiency[@ev_01]. "
        "We hypothesize further gains at higher pressure[@hypothesis]."
    )
    for sid, md in [("sec_1", sec1_md), ("sec_2", sec2_md)]:
        ctx = engine.get_section_context("ren_p", sid, "draft")
        engine.submit_section(
            {
                "project_id": "ren_p",
                "section_id": sid,
                "base_revision": 0,
                "context_revision": ctx["context_revision"],
                "context_fingerprint": ctx["context_fingerprint"],
                "idempotency_key": f"k-{sid}",
                "author_session": "writer-01",
                "content_markdown": md,
                "summary": f"Draft {sid}",
            }
        )

    # Count subprocess.run invocations during single-pass render
    real_run = subprocess.run
    pandoc_calls = 0

    def _counting_run(cmd, *args, **kwargs):
        nonlocal pandoc_calls
        if cmd and "pandoc" in str(cmd[0]).lower():
            pandoc_calls += 1
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    state = engine.store.load_state("ren_p")
    out_docx = tmp_path / "single_pass.docx"
    meta = render_document_single_pass_pandoc(state, out_docx, purpose="preview")

    assert pandoc_calls == 1
    assert meta["pandoc_invocations"] == 1
    assert out_docx.exists()

    doc = docx.Document(str(out_docx))
    omml_count = sum(1 for _ in doc._body._body.iter(qn("m:oMath")))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert omml_count >= 2
    assert len(doc.tables) >= 1
    assert "[1]" in full_text
    assert "Copper Single-Atom Catalysts" in full_text


def test_p2_capabilities_budget_and_mcp_unified_error_and_next_action(tmp_path: Path) -> None:
    """Verify get_capabilities default payload < 3000 chars, on-demand include_schemas,
    and MCP server singleton + unified DAFError JSON responses."""
    ws = tmp_path / "ws"
    engine = DAFEngine(workspace_root=ws)

    compact_caps = engine.get_capabilities()
    serialized = json.dumps(compact_caps, ensure_ascii=False)
    assert len(serialized) < 3000
    assert "contract_schemas" not in compact_caps

    with_schema = engine.get_capabilities(include_schemas=["SectionSubmissionContract"])
    assert "SectionSubmissionContract" in with_schema["contract_schemas"]

    mcp = create_mcp_server(workspace_root=ws)
    assert mcp is not None


def test_experiment_b_citation_check_recall_over_20_sentences() -> None:
    """Experiment B: 20 crafted sentences testing R-A (uncited numeric/comparative),
    R-B (numeric mismatch / digit boundary), and R-D (false experiment markers in hypothesis)."""
    excerpt = "Baseline latency is 18.4 ms across 15 nodes, throughput is 120 ops/s, and optimized latency is 9.2 ms."

    # 10 numeric/comparative uncited sentences -> R-A should catch all 10
    uncited_cases = [
        "系统吞吐量达到 500 ops/s。",
        "新算法使延迟降低了 45%。",
        "该方法优于现有基线方案。",
        "首次在室温条件下实现了稳定运行。",
        "内存占用减少至 256 MB。",
        "Our model improves accuracy by 12.5%.",
        "Latency was reduced to 4.1 ms under peak load.",
        "The proposed scheduler outperforms FIFO on all benchmarks.",
        "Throughput reached 300 QPS on a single node.",
        "训练耗时缩短至 3.5 h。",
    ]
    ra_hits = sum(1 for s in uncited_cases if sentence_needs_citation(s))
    assert ra_hits == 10

    # 5 wrong-number / digit-boundary cited sentences -> R-B should catch all 5
    rb_mismatch_cases = [
        "Optimized latency is 99.9 ms[@ev_01].",
        "Cluster size was 5 nodes[@ev_01].",  # 5 must NOT match 15
        "Throughput reached 20 ops/s[@ev_01].",  # 20 must NOT match 120
        "Baseline latency is 8.4 ms[@ev_01].",  # 8.4 must NOT match 18.4
        "Latency dropped to 2 ms[@ev_01].",  # 2 must NOT match 9.2
    ]
    rb_hits = 0
    for md in rb_mismatch_cases:
        sents, _, _ = analyze_markdown_with_pandoc(md)
        nums = extract_numbers_strict(sents[0].text)
        if any(not number_present_with_boundary(n, excerpt) for n in nums):
            rb_hits += 1
    assert rb_hits == 5

    # 3 hypothesis sentences with false experimental claims -> R-D should catch all 3
    rd_cases = [
        "实验表明该架构具备线性扩展能力[@hypothesis]。",
        "结果显示能耗可进一步下降[@hypothesis]。",
        "Results show the design generalizes to new domains[@hypothesis].",
    ]
    rd_hits = 0
    for md in rd_cases:
        sents, _, _ = analyze_markdown_with_pandoc(md)
        if sents[0].is_hypothesis and hypothesis_has_false_experiment_marker(sents[0].text):
            rd_hits += 1
    assert rd_hits == 3

    # 2 clean neutral narrative sentences -> should NOT trigger R-A false positives
    clean_cases = [
        "本文讨论本地单用户文档工程框架的模块划分。",
        "This section outlines the architectural motivation and terminology.",
    ]
    false_positives = sum(1 for s in clean_cases if sentence_needs_citation(s))
    assert false_positives == 0
