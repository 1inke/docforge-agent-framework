"""Round-2 R1, R2 & R7-B regression tests driven strictly via Agent tools, user APIs, CLI & UI."""

from __future__ import annotations

from pathlib import Path
import struct
import subprocess
import zlib

import docx
from docx.oxml.ns import qn
from docx.shared import Inches
from fastapi.testclient import TestClient
import pytest

from daf import DAFEngine
from daf.core.contracts import LayoutProfile
from daf.core.errors import (
    BibliographyMissingError,
    CrossrefUnresolvedError,
    SourcePolicyChangeRequiresHumanError,
    TemplateRenderError,
)
from daf.document_engine.pandoc_renderer import build_reference_docx
from daf.interfaces.cli.main import run_cli_args
from daf.interfaces.local_ui.app import create_local_ui_app
from tests.helpers import assert_clean_rendered_docx

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "adversarial"


def _write_tiny_png(path: Path) -> None:
    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

    ihdr = struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    path.write_bytes(png)


def _complete_review_and_final_approval(
    engine: DAFEngine,
    project_id: str,
    quotes_by_section: dict[str, list[str]],
) -> None:
    A = engine.call_agent_tool
    rv = A("start_review", {"project_id": project_id, "reviewer_id": "reviewer-indep-1"})
    st = engine.store.load_state(project_id)
    for task in rv["claimed_tasks"]:
        sid = task["section_id"]
        bindings = st.section_versions.sections[sid].source_bindings
        q_list = quotes_by_section[sid]
        chk = [
            {
                "check_id": item["check_id"],
                "question": item["question"],
                "verdict": "pass",
                "rationale": f"Checked [{item['check_id']}:{item['question'][:20]}] against drafted prose and source excerpt.",
                "quotes": [q_list[idx % len(q_list)]],
                "evidence_ids": [bindings[0].evidence_id] if bindings and bindings[0].evidence_id else [],
            }
            for idx, item in enumerate(task["checklist_items"])
        ]
        cev = [
            {
                "binding_id": b.binding_id,
                "semantic_support_status": "supported",
                "rationale": "Verified quantitative figures and wording against excerpt.",
            }
            for b in bindings
            if b.claim_type != "research_hypothesis"
        ]
        sub = A(
            "submit_review",
            {
                "project_id": project_id,
                "task_id": task["task_id"],
                "section_revision": task["target_section_revision"],
                "context_fingerprint": task["target_context_fingerprint"],
                "findings": [],
                "review_summary": "Independent review verified section claims and formatting.",
                "recommendation": "approve",
                "reviewed_by": "reviewer-indep-1",
                "claim_evaluations": cev,
                "checklist_results": chk,
                "checklist_hash": task["checklist_hash"],
            },
        )
        assert sub["status"] == "accepted"

    freq = A("request_approval", {"project_id": project_id, "artifact_type": "final_draft"})
    engine.decide_approval_by_human(
        project_id=project_id,
        approval_request_id=freq["approval_request_id"],
        decision="approved",
        confirmation_channel="local_cli",
    )


def test_r1_bibliography_governance_propose_ui_cli_and_source_dedup(tmp_path: Path) -> None:
    """E7 regression: preview shows visible placeholder (no fabricated author/year); release blocks until
    confirmed via UI/CLI; same source cited by two evidence items produces 1 deduplicated GB/T 7714 entry."""
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    src_file = src_dir / "same_source.md"
    src_file.write_text((FIXTURES_DIR / "same_source_two_evidence.md").read_text(encoding="utf-8"), encoding="utf-8")

    ws = tmp_path / "ws"
    prof_dir = ws / "profiles"
    prof_dir.mkdir(parents=True)
    (prof_dir / "p1_prof.yaml").write_text(
        """profile_id: p1_prof
display_name: R1 Profile
sections:
  - id: s1
    title: 1. 性能分析
    required: true
    words: [5, 2000]
    review_checklist:
      - 引用指标是否均源自同一已登记来源？
""",
        encoding="utf-8",
    )

    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_r1", "title": "R1 文献治理测试", "profile_id": "p1_prof"})
    ing = A("ingest_sources", {"project_id": "p_r1", "source_paths": [str(src_file)]})
    src_id = ing["ingested_sources"][0]["source_id"]
    sl_id = A("find_sources", {"project_id": "p_r1", "query": "18.4 ms", "source_ids": [src_id]})["matches"][0]["slice_id"]

    A(
        "register_evidence",
        {
            "project_id": "p_r1",
            "evidence_items": [
                {
                    "evidence_id": "ev_01",
                    "source_id": src_id,
                    "slice_id": sl_id,
                    "quote_excerpt": "基线系统在集群规模为 15 节点时的平均时延为 18.4 ms",
                },
                {
                    "evidence_id": "ev_02",
                    "source_id": src_id,
                    "slice_id": sl_id,
                    "quote_excerpt": "本文提出的轻量调度方法使平均时延降低到 9.2 ms，吞吐量达到 3.2 GB/s",
                },
            ],
        },
    )
    A("submit_outline", {"project_id": "p_r1", "global_thesis": "t", "nodes": [{"section_id": "s1", "title": "1. 性能分析", "assigned_evidence_ids": ["ev_01", "ev_02"]}]})
    oreq = A("request_approval", {"project_id": "p_r1", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_r1", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")

    ctx = A("get_section_context", {"project_id": "p_r1", "section_id": "s1", "action": "draft"})
    A(
        "submit_section",
        {
            "project_id": "p_r1",
            "section_id": "s1",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "r1-s1",
            "author_session": "author-01",
            "content_markdown": (
                "基线系统在集群规模为 15 节点时的平均时延为 18.4 ms[@ev_01]。"
                "本文方法将平均时延降至 9.2 ms，吞吐量达 3.2 GB/s[@ev_02]。"
            ),
            "summary": "Draft s1",
        },
    )
    _complete_review_and_final_approval(
        eng,
        "p_r1",
        {"s1": ["基线系统在集群规模为 15 节点时的平均时延为 18.4 ms[@ev_01]。"]},
    )

    # Preview works before bibliography confirmation and shows visible placeholder
    prev = A("render_document", {"project_id": "p_r1", "purpose": "preview"})
    prev_txt = assert_clean_rendered_docx(
        Path(prev["artifact_path"]),
        expected_bib_count=1,
        allow_preview_placeholder=True,
    )
    assert "【未登记文献信息：same_source.md】" in prev_txt
    assert "2026" not in prev_txt

    # Release blocked until user confirms bibliography
    with pytest.raises(BibliographyMissingError) as exc_bib:
        A("render_document", {"project_id": "p_r1", "purpose": "release"})
    assert exc_bib.value.code == "BIBLIOGRAPHY_MISSING"

    # Agent proposes bibliography (cannot confirm on its own)
    prop = A(
        "propose_source_bibliography",
        {
            "project_id": "p_r1",
            "source_id": src_id,
            "csl_json": {
                "type": "article-journal",
                "title": "轻量异构调度机制分析",
                "author": [{"family": "张", "given": "三"}],
                "issued": {"date-parts": [[2024]]},
            },
        },
    )
    assert prop["confirmed"] is False
    with pytest.raises(BibliographyMissingError):
        A("render_document", {"project_id": "p_r1", "purpose": "release"})

    for blocked in ("set_source_bibliography_by_user", "adopt_source_bibliography_by_user"):
        with pytest.raises(SourcePolicyChangeRequiresHumanError):
            A(blocked, {"project_id": "p_r1", "source_id": src_id})

    # Local UI adopts proposal
    ui_app, tok = create_local_ui_app(workspace_root=ws, allowed_source_roots=[src_dir])
    client = TestClient(ui_app)
    assert "Adopt Proposal" in client.get(f"/projects/p_r1?token={tok}").text
    assert client.post(f"/projects/p_r1/sources/{src_id}/adopt-bibliography?token={tok}").status_code == 200

    # CLI overrides via BibTeX -> Pandoc CSL-JSON
    bib_file = tmp_path / "refs.bib"
    bib_file.write_text(
        "@article{zhang2025,\n"
        "  title={Oriented Low-Latency Cluster Scheduling},\n"
        "  author={Zhang, San and Li, Si},\n"
        "  journal={Journal of Software},\n"
        "  year={2025},\n"
        "  volume={36},\n"
        "  pages={100--112}\n"
        "}\n",
        encoding="utf-8",
    )
    bib_out = run_cli_args(["--workspace", str(ws), "sources", "set-bib", "p_r1", src_id, "--bibtex", str(bib_file)])
    assert bib_out["bibliography"]["id"] == src_id

    rel = A("render_document", {"project_id": "p_r1", "purpose": "release"})
    rel_txt = assert_clean_rendered_docx(Path(rel["artifact_path"]), expected_bib_count=1)
    assert "oriented low-latency cluster scheduling" in rel_txt.lower()


def test_r2_crossref_hypothesis_and_unresolved_gate(tmp_path: Path) -> None:
    """R2 regression: forward/backward @tbl/@fig/@eq crossrefs resolve, [@hypothesis] is stripped,
    and undefined @fig:missing blocks release with CROSSREF_UNRESOLVED."""
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    (src_dir / "bench.md").write_text("基线时延为 18.4 ms，本文方法达到 9.2 ms。\n", encoding="utf-8")

    ws = tmp_path / "ws"
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_r2", "title": "R2 交叉引用测试"})
    _write_tiny_png(eng.store.ensure_project_dirs("p_r2") / "arch.png")

    ing = A("ingest_sources", {"project_id": "p_r2", "source_paths": [str(src_dir / "bench.md")]})
    sid = ing["ingested_sources"][0]["source_id"]
    sl = A("list_source_blocks", {"project_id": "p_r2", "source_id": sid})["blocks"][0]
    A(
        "register_evidence",
        {
            "project_id": "p_r2",
            "evidence_items": [
                {
                    "evidence_id": "ev_01",
                    "source_id": sid,
                    "slice_id": sl["slice_id"],
                    "quote_excerpt": "基线时延为 18.4 ms，本文方法达到 9.2 ms",
                }
            ],
        },
    )
    eng.set_source_bibliography_by_user(
        "p_r2",
        sid,
        {"type": "article-journal", "title": "CrossRef Benchmark", "author": [{"family": "Wang"}], "issued": {"date-parts": [[2025]]}},
    )
    A("submit_outline", {"project_id": "p_r2", "global_thesis": "t", "nodes": [{"section_id": "s1", "title": "1. 实验设置", "target_min_words": 5, "assigned_evidence_ids": ["ev_01"]}]})
    oreq = A("request_approval", {"project_id": "p_r2", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_r2", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")

    # 1) Undefined @fig:missing blocks release
    ctx0 = A("get_section_context", {"project_id": "p_r2", "section_id": "s1", "action": "draft"})
    A(
        "submit_section",
        {
            "project_id": "p_r2",
            "section_id": "s1",
            "base_revision": 0,
            "context_revision": ctx0["context_revision"],
            "context_fingerprint": ctx0["context_fingerprint"],
            "idempotency_key": "bad-xref",
            "author_session": "author-01",
            "content_markdown": "参见缺失图 @fig:missing，测量结果基线为 18.4 ms[@ev_01]。",
            "summary": "Bad crossref test",
        },
    )
    _complete_review_and_final_approval(
        eng,
        "p_r2",
        {"s1": ["参见缺失图 @fig:missing，测量结果基线为 18.4 ms[@ev_01]。"]},
    )
    with pytest.raises(CrossrefUnresolvedError) as exc_xr:
        A("render_document", {"project_id": "p_r2", "purpose": "release"})
    assert exc_xr.value.code == "CROSSREF_UNRESOLVED"

    # 2) Valid adversarial crossref_and_hypothesis.md resolves forward/backward & strips [@hypothesis]
    xref_md = (FIXTURES_DIR / "crossref_and_hypothesis.md").read_text(encoding="utf-8")
    ctx1 = A("get_section_context", {"project_id": "p_r2", "section_id": "s1", "action": "revise"})
    A(
        "submit_section",
        {
            "project_id": "p_r2",
            "section_id": "s1",
            "base_revision": 1,
            "context_revision": ctx1["context_revision"],
            "context_fingerprint": ctx1["context_fingerprint"],
            "idempotency_key": "good-xref",
            "author_session": "author-01",
            "content_markdown": xref_md,
            "summary": "Valid forward and backward crossrefs",
        },
    )
    _complete_review_and_final_approval(
        eng,
        "p_r2",
        {"s1": ["前向引用见 @tbl:lat、@fig:arch 与式 @eq:energy，本文拟进一步降低尾部时延[@hypothesis]。"]},
    )
    rel = A("render_document", {"project_id": "p_r2", "purpose": "release"})
    txt = assert_clean_rendered_docx(Path(rel["artifact_path"]), expected_bib_count=1)
    assert "表 1" in txt and "图 1" in txt and "式 (1)" in txt
    assert "[@hypothesis]" not in txt and "hypothesis" not in txt


def test_r7b_mapped_single_pass_tagged_rejected_and_reference_docx_styling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7-B regression: mapped mode uses 1 Pandoc invocation with consistent bib/crossref;
    tagged mode raises TEMPLATE_MODE_UNSUPPORTED; build_reference_docx sets margins & eastAsia fonts."""
    ref_out = tmp_path / "ref.docx"
    custom_layout = LayoutProfile(
        cjk_font="仿宋",
        heading_cjk_font="黑体",
        latin_font="Calibri",
        body_font_size_pt=11.0,
        margin_top_in=0.9,
        margin_bottom_in=0.9,
        margin_left_in=1.1,
        margin_right_in=1.1,
    )
    build_reference_docx(custom_layout, ref_out)
    ref_doc = docx.Document(str(ref_out))
    assert abs(ref_doc.sections[0].top_margin - Inches(0.9)) < 2000
    assert abs(ref_doc.sections[0].left_margin - Inches(1.1)) < 2000
    norm_rfonts = ref_doc.styles["Normal"]._element.rPr.rFonts
    assert norm_rfonts.get(qn("w:eastAsia")) == "仿宋"
    h1_rfonts = ref_doc.styles["Heading 1"]._element.rPr.rFonts
    assert h1_rfonts.get(qn("w:eastAsia")) == "黑体"

    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    (src_dir / "data.md").write_text("Latency 9.2 ms, throughput 3.2 GB/s.\n", encoding="utf-8")
    tpl = src_dir / "tpl.docx"
    tdoc = docx.Document()
    tdoc.add_heading("1. Method", level=1)
    tbl = tdoc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "Metric"
    tbl.cell(0, 1).text = "Detail"
    tbl.cell(1, 0).text = "Results"
    tbl.cell(1, 1).text = ""
    tdoc.save(str(tpl))

    ws = tmp_path / "ws"
    eng = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    A = eng.call_agent_tool
    A("create_project", {"project_id": "p_map", "title": "Mapped Mode Doc"})
    ing = A("ingest_sources", {"project_id": "p_map", "source_paths": [str(src_dir / "data.md")], "template_path": str(tpl)})
    sid = ing["ingested_sources"][0]["source_id"]
    sl = A("list_source_blocks", {"project_id": "p_map", "source_id": sid})["blocks"][0]
    A(
        "register_evidence",
        {
            "project_id": "p_map",
            "evidence_items": [
                {"evidence_id": "ev_1", "source_id": sid, "slice_id": sl["slice_id"], "quote_excerpt": "Latency 9.2 ms, throughput 3.2 GB/s"}
            ],
        },
    )
    eng.set_source_bibliography_by_user(
        "p_map",
        sid,
        {"type": "article-journal", "title": "Unified Mapped Bibliography Entry", "author": [{"family": "Zhao"}], "issued": {"date-parts": [[2025]]}},
    )
    A("submit_outline", {"project_id": "p_map", "global_thesis": "t", "nodes": [{"section_id": "sec_1", "title": "1. Method"}, {"section_id": "sec_2", "title": "2. Results"}]})
    oreq = A("request_approval", {"project_id": "p_map", "artifact_type": "outline"})
    eng.decide_approval_by_human("p_map", oreq["approval_request_id"], "approved", confirmation_channel="local_cli")

    c1 = A("get_section_context", {"project_id": "p_map", "section_id": "sec_1", "action": "draft"})
    A("submit_section", {"project_id": "p_map", "section_id": "sec_1", "base_revision": 0, "context_revision": c1["context_revision"], "context_fingerprint": c1["context_fingerprint"], "idempotency_key": "m-s1", "author_session": "writer-1", "content_markdown": "如表 @tbl:m 所示，延迟为 9.2 ms[@ev_1]，能量关系 [$$ E = mc^2 $$]{#eq:e1}。", "summary": "S1"})
    c2 = A("get_section_context", {"project_id": "p_map", "section_id": "sec_2", "action": "draft"})
    A("submit_section", {"project_id": "p_map", "section_id": "sec_2", "base_revision": 0, "context_revision": c2["context_revision"], "context_fingerprint": c2["context_fingerprint"], "idempotency_key": "m-s2", "author_session": "writer-1", "content_markdown": "由式 @eq:e1 可见吞吐量达到 3.2 GB/s[@ev_1]。\n\n::: {#tbl:m}\n| 指标 | 数值 |\n|---|---|\n| 吞吐 | 3.2 GB/s |\n:::\n", "summary": "S2"})

    with pytest.raises(TemplateRenderError) as exc_tag:
        A("render_document", {"project_id": "p_map", "purpose": "preview", "template_mode": "tagged"})
    assert exc_tag.value.code == "TEMPLATE_MODE_UNSUPPORTED"

    real_run = subprocess.run
    pandoc_calls = [0]

    def _counting_run(cmd, *args, **kwargs):
        if cmd and "pandoc" in str(cmd[0]).lower():
            pandoc_calls[0] += 1
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    res = A(
        "render_document",
        {
            "project_id": "p_map",
            "purpose": "preview",
            "template_mode": "mapped",
            "section_mapping": {"sec_1": "heading:1. Method", "sec_2": "table:0:r1:c1"},
        },
    )
    assert pandoc_calls[0] == 1
    out_txt = assert_clean_rendered_docx(Path(res["artifact_path"]), expected_bib_count=1)
    assert "表 1" in out_txt and "式 (1)" in out_txt and "Unified Mapped Bibliography Entry" in out_txt
