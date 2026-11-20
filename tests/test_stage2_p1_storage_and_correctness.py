"""Stage 2 (P1-1, P1-2, P1-4) storage, read-only query, performance, lease, and idempotency tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import pytest

from daf import DAFEngine
from daf.core.contracts import RenderJob
from daf.core.errors import IdempotencyKeyConflictError, ProjectNotFoundError


def test_p1_1_readonly_queries_do_not_bump_version_and_write_latency_is_bounded(tmp_path: Path) -> None:
    """Experiment 1 regression: 2.8 MB source ingested -> read queries don't bump state_version,
    and subsequent metadata writes stay fast (not rewriting multi-MB source text)."""
    src_dir = tmp_path / "sources"
    src_dir.mkdir()
    big_file = src_dir / "large_corpus.md"
    # ~2.8 MB source file
    chunk = "# Section\nThe measured throughput reached 128.5 ops/s with 9.2 ms median latency.\n" * 400
    big_file.write_text(chunk * 100, encoding="utf-8")
    assert big_file.stat().st_size > 2_500_000

    ws = tmp_path / "ws"
    engine = DAFEngine(workspace_root=ws, allowed_source_roots=[src_dir])
    engine.create_project(project_id="perf_p1", title="Perf Test")
    engine.ingest_sources(project_id="perf_p1", source_paths=[str(big_file)])

    # Measure metadata write latency under 2.8 MB corpus (submit_outline + request_approval)
    t0 = time.perf_counter()
    engine.submit_outline(
        project_id="perf_p1",
        global_thesis="Compact state JSON keeps writes fast.",
        nodes=[{"section_id": "sec_1", "title": "1. Intro", "writing_objectives": ["Intro"]}],
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    req = engine.request_approval(project_id="perf_p1", artifact_type="outline")

    v_before = engine.get_project_status("perf_p1")["state_version"]
    for _ in range(20):
        st = engine.get_approval_status(
            project_id="perf_p1",
            approval_request_id=req["approval_request_id"],
        )
        assert st["status"] == "pending"
        engine.get_project_status("perf_p1")
    v_after = engine.get_project_status("perf_p1")["state_version"]
    assert v_after == v_before

    # Verify compact state_json in SQLite is small (< 100 KB) despite 2.8 MB source corpus
    from contextlib import closing

    with closing(engine.store._connect(readonly=True)) as conn:
        row = conn.execute("SELECT length(state_json) AS sz FROM projects WHERE project_id = 'perf_p1'").fetchone()
        blob_count = conn.execute("SELECT count(*) AS cnt FROM blobs").fetchone()["cnt"]
    assert int(row["sz"]) < 100_000
    assert int(blob_count) >= 1
    assert elapsed_ms < 250.0  # Compact write avoids MB-scale serialization


def test_p1_1_no_json_resurrection_after_db_deletion(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    engine = DAFEngine(workspace_root=ws)
    engine.create_project(project_id="ghost_p", title="No Resurrection")
    snap = engine.store.export_snapshot("ghost_p")
    assert snap.exists()

    # Delete SQLite DB (and WAL/SHM if any)
    for f in ws.glob("daf_workspace.sqlite3*"):
        f.unlink()

    with pytest.raises(ProjectNotFoundError) as exc_info:
        engine.get_project_status("ghost_p")
    assert exc_info.value.code == "PROJECT_NOT_FOUND"


def test_p1_4_active_render_job_not_reaped_by_status_query(tmp_path: Path) -> None:
    """Experiment 3 regression: get_project_status never reaps active render jobs, and
    recover_render_jobs only reaps jobs whose lease expired AND owner PID is dead."""
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

    # Status query must NOT reap either job or delete tmp_file
    status = engine.get_project_status("rjob_p")
    assert len(status["active_render_jobs"]) == 2
    assert tmp_file.exists()

    # Explicit recovery reaps ONLY the expired dead job, keeping the leased active job intact
    rec = engine.recover_render_jobs("rjob_p")
    assert rec["recovered_jobs"] == ["rjob_dead"]
    assert tmp_file.exists()
    state_after = engine.store.load_state("rjob_p", hydrate_slices=False, hydrate_sections=False)
    assert state_after.render_jobs["rjob_live"].phase == "built"
    assert state_after.render_jobs["rjob_dead"].phase == "aborted_recovered"


def test_p1_4_idempotency_and_approval_deduplication(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    engine = DAFEngine(workspace_root=ws)
    engine.create_project(project_id="idem_p", title="Idempotency Test")

    r1 = engine.submit_outline(
        project_id="idem_p",
        global_thesis="Thesis v1",
        nodes=[{"section_id": "s1", "title": "1. S1"}],
        idempotency_key="out-k1",
    )
    r2 = engine.submit_outline(
        project_id="idem_p",
        global_thesis="Thesis v1",
        nodes=[{"section_id": "s1", "title": "1. S1"}],
        idempotency_key="out-k1",
    )
    assert r2["idempotent_replay"] is True
    assert r2["outline_revision"] == r1["outline_revision"]

    with pytest.raises(IdempotencyKeyConflictError):
        engine.submit_outline(
            project_id="idem_p",
            global_thesis="Different thesis",
            nodes=[{"section_id": "s1", "title": "1. S1"}],
            idempotency_key="out-k1",
        )

    # Approval deduplication for identical artifact_hash
    a1 = engine.request_approval(project_id="idem_p", artifact_type="outline")
    a2 = engine.request_approval(project_id="idem_p", artifact_type="outline")
    assert a1["approval_request_id"] == a2["approval_request_id"]
