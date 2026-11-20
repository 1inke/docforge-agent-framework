"""Unit tests directly exercising ProjectStore transactions, blob LRU cache, and doctor checks."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from daf import DAFEngine
from daf.core.contracts import RenderJob
from daf.core.errors import StateCorruptedError
from daf.interfaces.cli.main import run_cli_args
from daf.project_store.store import _BlobLRUCache

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "adversarial"


def _setup_approved_outline(tmp_path: Path, project_id: str = "p1") -> tuple[DAFEngine, Path]:
    src_dir = tmp_path / "sources"
    src_dir.mkdir(exist_ok=True)
    ws = tmp_path / "ws"
    engine = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    engine.create_project(project_id=project_id, title="Store Unit Test")
    engine.submit_outline(
        project_id=project_id,
        global_thesis="Store integrity test thesis.",
        nodes=[{"section_id": "s1", "title": "S1"}],
    )
    req = engine.request_approval(project_id=project_id, artifact_type="outline")
    engine.decide_approval_by_human(
        project_id=project_id,
        approval_request_id=req["approval_request_id"],
        decision="approved",
        confirmation_channel="local_cli",
    )
    return engine, ws


def test_p1_4_active_render_job_not_reaped_by_status_query(tmp_path: Path) -> None:
    """Status queries never reap active render jobs; recovery reaps only expired dead-PID jobs."""
    ws = tmp_path / "ws"
    engine = DAFEngine(workspace_root=ws)
    engine.create_project(project_id="rjob_p", title="Render Lease Test")
    artifacts = engine.store.ensure_project_dirs("rjob_p") / "artifacts"
    tmp_file = artifacts / "active.tmp.docx"
    tmp_file.write_bytes(b"in-progress")

    now = datetime.now(timezone.utc)
    active_job = RenderJob(
        job_id="rjob_live",
        render_id="render_0001",
        project_id="rjob_p",
        purpose="preview",
        phase="built",
        input_fingerprint="sha256:abc",
        temp_path=str(tmp_file),
        target_path=str(artifacts / "out.docx"),
        owner_pid=None,
        heartbeat_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=10)).isoformat(),
    )
    expired_dead_job = RenderJob(
        job_id="rjob_dead",
        render_id="render_0002",
        project_id="rjob_p",
        purpose="preview",
        phase="prepared",
        input_fingerprint="sha256:def",
        temp_path="",
        target_path="",
        owner_pid=999_999_999,
        heartbeat_at=(now - timedelta(hours=1)).isoformat(),
        lease_until=(now - timedelta(minutes=30)).isoformat(),
    )

    def _inject(state, _conn):
        state.render_jobs[active_job.job_id] = active_job
        state.render_jobs[expired_dead_job.job_id] = expired_dead_job
        return {}, "Injected render jobs for lease test.", {}

    engine.store.transact_project("rjob_p", "test_inject", _inject)

    status = engine.get_project_status("rjob_p")
    assert len(status["active_render_jobs"]) == 2
    assert tmp_file.exists()

    rec = engine.recover_render_jobs("rjob_p")
    assert rec["recovered_jobs"] == ["rjob_dead"]
    assert tmp_file.exists()
    state_after = engine.store.load_state("rjob_p", hydrate_slices=False, hydrate_sections=False)
    assert state_after.render_jobs["rjob_live"].phase == "built"
    assert state_after.render_jobs["rjob_dead"].phase == "aborted_recovered"


def test_r4_rollback_then_retry_persists_blob_for_fresh_engine(tmp_path: Path) -> None:
    """Mid-tx rollback must not poison in-memory blob cache; retry must write blob and be readable by fresh engine."""
    engine, ws = _setup_approved_outline(tmp_path, project_id="p_blob")
    prose = (FIXTURES_DIR / "rollback_retry.md").read_text(encoding="utf-8").strip()

    ctx = engine.get_section_context("p_blob", "s1", "draft")
    submission = {
        "project_id": "p_blob",
        "section_id": "s1",
        "base_revision": 0,
        "context_revision": ctx["context_revision"],
        "context_fingerprint": ctx["context_fingerprint"],
        "idempotency_key": "blob-retry-k1",
        "author_session": "writer-1",
        "content_markdown": prose,
        "summary": "Rollback test",
    }

    with pytest.raises(RuntimeError, match="Simulated mid-transaction crash"):
        engine.submit_section(submission, simulate_mid_transaction_crash=True)

    # Same payload retried succeeds and commits blob row
    res = engine.submit_section(submission, simulate_mid_transaction_crash=False)
    assert res["new_revision"] == 1
    assert engine.store.load_state("p_blob").section_versions.sections["s1"].content_markdown == prose

    # Fresh engine with cold LRU cache must also read complete prose from SQLite
    fresh = DAFEngine(workspace_root=ws)
    assert fresh.store.load_state("p_blob").section_versions.sections["s1"].content_markdown == prose

    with closing(sqlite3.connect(ws / "daf_workspace.sqlite3")) as conn:
        blob_rows = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    assert blob_rows >= 1

    doc_res = run_cli_args(["--workspace", str(ws), "doctor", "p_blob"])
    assert doc_res["ok"] is True
    assert doc_res["healthy"] is True
    assert doc_res["missing_blobs"] == []


def test_r4_deleted_blob_raises_state_corrupted_and_doctor_reports_it(tmp_path: Path) -> None:
    """Deleting a referenced blob causes load_state to raise STATE_CORRUPTED and daf doctor to report it."""
    engine, ws = _setup_approved_outline(tmp_path, project_id="p_corrupt")
    ctx = engine.get_section_context("p_corrupt", "s1", "draft")
    engine.submit_section(
        {
            "project_id": "p_corrupt",
            "section_id": "s1",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "corrupt-1",
            "author_session": "writer-1",
            "content_markdown": "该段落内容用于测试缺失内容块检测。",
            "summary": "Draft for corruption check",
        }
    )
    chash = engine.store.load_state("p_corrupt").section_versions.sections["s1"].content_hash
    assert chash

    # Wipe the blob row directly and use a fresh engine to bypass LRU cache
    with closing(sqlite3.connect(ws / "daf_workspace.sqlite3")) as conn:
        conn.execute("DELETE FROM blobs WHERE sha256 = ?", (chash,))
        conn.commit()

    fresh = DAFEngine(workspace_root=ws)
    with pytest.raises(StateCorruptedError) as exc_info:
        fresh.store.load_state("p_corrupt")
    assert exc_info.value.code == "STATE_CORRUPTED"
    assert any(m["sha256"] == chash for m in exc_info.value.details["missing_blobs"])

    doc_res = run_cli_args(["--workspace", str(ws), "doctor", "p_corrupt"])
    assert doc_res["ok"] is False
    assert any(m["sha256"] == chash for m in doc_res["missing_blobs"])


def test_r4_blob_lru_cache_eviction_and_sqlite_reload(tmp_path: Path) -> None:
    cache = _BlobLRUCache(max_bytes=20)
    cache.put("sha1", "0123456789")  # 10 bytes
    cache.put("sha2", "abcdefghij")  # 10 bytes (total 20)
    assert cache.get("sha1") == "0123456789"  # sha1 promoted to MRU, sha2 now LRU
    cache.put("sha3", "ZZZZZZZZZZ")  # 10 bytes -> evicts sha2
    assert cache.get("sha2") is None
    assert cache.get("sha1") == "0123456789"
    assert cache.get("sha3") == "ZZZZZZZZZZ"

    # Also verify SQLite fallback re-hydrates when LRU cache drops an entry on live ProjectStore
    engine, _ws = _setup_approved_outline(tmp_path, project_id="p_lru")
    engine.store._blob_cache = _BlobLRUCache(max_bytes=64)
    ctx = engine.get_section_context("p_lru", "s1", "draft")
    text1 = "First section body text exceeding sixty four bytes so it overflows tiny cache or evicts earlier entry cleanly!"
    engine.submit_section(
        {
            "project_id": "p_lru",
            "section_id": "s1",
            "base_revision": 0,
            "context_revision": ctx["context_revision"],
            "context_fingerprint": ctx["context_fingerprint"],
            "idempotency_key": "lru-1",
            "author_session": "writer-1",
            "content_markdown": text1,
            "summary": "LRU check",
        }
    )
    assert engine.store.load_state("p_lru").section_versions.sections["s1"].content_markdown == text1
