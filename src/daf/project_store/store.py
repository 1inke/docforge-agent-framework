"""Project Workspace & SQLite Atomic CAS State Store for DocForge Agent Framework (DAF §3.2, §7.2, §9, DAF-P0-03 & R2)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any

from daf.core.contracts import ApprovalDecision, ApprovalRequest, ProjectWorkspaceState
from daf.core.errors import (
    ApprovalConflictError,
    ArtifactPathInvalidError,
    IdempotencyKeyConflictError,
    ProjectAlreadyExistsError,
    RevisionConflictError,
    SecurityBoundaryError,
    UntrustedApprovalChannelError,
)

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")
SAFE_DOCX_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff][A-Za-z0-9_\-. \u4e00-\u9fff]{0,120}\.docx$", re.IGNORECASE)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}

WORKSPACE_SUBDIRS: tuple[str, ...] = (
    "sources",
    "profiles",
    "outline",
    "sections",
    "evidence",
    "reviews",
    "decisions",
    "artifacts",
    "logs",
)

DB_SCHEMA_VERSION = 3


def validate_safe_identifier(value: str, field_name: str = "identifier") -> str:
    """Reject path traversal characters and unsafe identifiers."""
    if not value or not isinstance(value, str):
        raise SecurityBoundaryError(f"Invalid empty {field_name}.")
    if ".." in value or "/" in value or "\\" in value or ":" in value:
        raise SecurityBoundaryError(
            f"Security violation: {field_name} '{value}' contains path traversal or separator characters."
        )
    if not SAFE_ID_PATTERN.match(value):
        raise SecurityBoundaryError(
            f"Security violation: {field_name} '{value}' must match {SAFE_ID_PATTERN.pattern}."
        )
    return value


def validate_safe_docx_filename(filename: str) -> str:
    """Validate that `output_filename` is a single safe `.docx` filename with no traversal or reserved names (R2-P1-05)."""
    if not filename or not isinstance(filename, str):
        raise ArtifactPathInvalidError(
            "Invalid empty output_filename.",
            details={"output_filename": filename},
        )
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or ":" in filename
        or any(ord(ch) < 32 for ch in filename)
    ):
        raise ArtifactPathInvalidError(
            f"Security violation: output_filename '{filename}' contains path traversal, drive/stream syntax, or separator characters.",
            details={"output_filename": filename},
        )
    if filename.endswith(".") or filename.endswith(" "):
        raise ArtifactPathInvalidError(
            f"Security violation: output_filename '{filename}' cannot end with a dot or space.",
            details={"output_filename": filename},
        )
    stem = filename.split(".")[0].strip().upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ArtifactPathInvalidError(
            f"Security violation: output_filename '{filename}' uses Windows reserved device name '{stem}'.",
            details={"output_filename": filename},
        )
    if not filename.lower().endswith(".docx") or not SAFE_DOCX_FILENAME_PATTERN.match(filename):
        raise ArtifactPathInvalidError(
            f"Security violation: output_filename '{filename}' must be a valid single '.docx' filename.",
            details={"output_filename": filename},
        )
    return filename


def compute_canonical_request_hash(payload: Any) -> str:
    """Compute a deterministic SHA-256 hash over a canonical JSON representation of a request payload."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_human_portal_signature(
    secret_key: str,
    project_id: str,
    approval_request_id: str,
    artifact_hash: str,
    decision: str,
    human_actor_id: str,
) -> str:
    """Compute HMAC-SHA256 signature for out-of-band human confirmation (R2-P0-01)."""
    msg = f"{project_id}:{approval_request_id}:{artifact_hash}:{decision}:{human_actor_id}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class ProjectStore:
    """Manages project workspace directories, SQLite atomic CAS transactions, idempotency, and derived JSON snapshots."""

    def __init__(self, workspace_root: str | Path) -> None:
        raw_root = Path(workspace_root)
        if raw_root.exists() and raw_root.is_symlink():
            raise SecurityBoundaryError(
                f"Security violation: workspace_root '{raw_root}' cannot be a symbolic link."
            )
        self.workspace_root = raw_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace_root / "daf_workspace.sqlite3"
        self._init_and_migrate_db()

    def _connect(self, autocommit: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None if autocommit else "DEFERRED",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_and_migrate_db(self) -> None:
        conn = self._connect(autocommit=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    stage TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 0,
                    state_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    project_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    request_hash TEXT NOT NULL DEFAULT '',
                    result_state_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    PRIMARY KEY (project_id, idempotency_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_tokens (
                    token_hash TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    approved_revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                )
                """
            )

            # Check columns for migration from v1/v2 databases
            proj_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "state_version" not in proj_cols:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN state_version INTEGER NOT NULL DEFAULT 0"
                )

            idem_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(idempotency_records)").fetchall()
            }
            if "request_hash" not in idem_cols:
                conn.execute(
                    "ALTER TABLE idempotency_records ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''"
                )
            if "result_state_version" not in idem_cols:
                conn.execute(
                    "ALTER TABLE idempotency_records ADD COLUMN result_state_version INTEGER NOT NULL DEFAULT 0"
                )

            tok_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(approval_tokens)").fetchall()
            }
            if "token" in tok_cols and "token_hash" not in tok_cols:
                conn.execute("DROP TABLE approval_tokens")
                conn.execute(
                    """
                    CREATE TABLE approval_tokens (
                        token_hash TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        artifact_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        approved_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used_at TEXT
                    )
                    """
                )

            conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_project_dir(self, project_id: str) -> Path:
        validate_safe_identifier(project_id, "project_id")
        projects_parent = self.workspace_root / "projects"
        if projects_parent.exists() and projects_parent.is_symlink():
            raise SecurityBoundaryError(
                f"Security violation: projects directory '{projects_parent}' is a symbolic link."
            )
        raw_project_dir = projects_parent / project_id
        if raw_project_dir.exists() and raw_project_dir.is_symlink():
            raise SecurityBoundaryError(
                f"Security violation: project directory '{raw_project_dir}' is a symbolic link."
            )
        project_dir = raw_project_dir.resolve()
        try:
            project_dir.relative_to(self.workspace_root)
        except ValueError as exc:
            raise SecurityBoundaryError(
                f"Security violation: project path '{project_dir}' escapes workspace root '{self.workspace_root}'."
            ) from exc
        return project_dir

    def project_dir(self, project_id: str) -> Path:
        return self.get_project_dir(project_id)

    def ensure_project_dirs(self, project_id: str) -> Path:
        project_dir = self.get_project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        for sub in WORKSPACE_SUBDIRS:
            sub_dir = project_dir / sub
            if sub_dir.exists() and sub_dir.is_symlink():
                err_cls = ArtifactPathInvalidError if sub == "artifacts" else SecurityBoundaryError
                raise err_cls(
                    f"Security violation: workspace subdirectory '{sub_dir}' is a symbolic link."
                )
            sub_dir.mkdir(parents=True, exist_ok=True)
            resolved_sub = sub_dir.resolve()
            try:
                resolved_sub.relative_to(project_dir)
            except ValueError as exc:
                err_cls = ArtifactPathInvalidError if sub == "artifacts" else SecurityBoundaryError
                raise err_cls(
                    f"Security violation: subdirectory '{resolved_sub}' escapes project directory '{project_dir}'."
                ) from exc
        return project_dir

    def sync_derived_snapshots(self, state: ProjectWorkspaceState) -> None:
        """Write derived human-readable JSON files AFTER SQLite transaction commits.
        SQLite remains the sole authoritative state source (DAF-P0-03 §3.4).
        No secret approval tokens are ever written to disk (R2-P0-01 §1.3C).
        """
        project_id = validate_safe_identifier(state.brief.project_id, "project_id")
        project_dir = self.ensure_project_dirs(project_id)
        state_dict = state.model_dump(mode="json")
        state_json = json.dumps(state_dict, ensure_ascii=False, indent=2)

        try:
            (project_dir / "state_snapshot.json").write_text(state_json, encoding="utf-8")
            (project_dir / "profiles" / "document_profile.json").write_text(
                json.dumps(state.profile.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (project_dir / "sources" / "source_registry.json").write_text(
                json.dumps(state.source_registry.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (project_dir / "evidence" / "evidence_ledger.json").write_text(
                json.dumps(state.evidence_ledger.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (project_dir / "outline" / "outline_graph.json").write_text(
                json.dumps(state.outline_graph.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (project_dir / "decisions" / "decision_log.json").write_text(
                json.dumps(state.decision_log.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (project_dir / "decisions" / "approval_requests.json").write_text(
                json.dumps(
                    {k: v.model_dump(mode="json") for k, v in state.approval_requests.items()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (project_dir / "reviews" / "review_ledger.json").write_text(
                json.dumps(state.review_ledger.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if state.last_render_manifest is not None:
                (project_dir / "artifacts" / "render_manifest.json").write_text(
                    json.dumps(state.last_render_manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            for sec_id, sec_state in state.section_versions.sections.items():
                safe_sec_id = validate_safe_identifier(sec_id, "section_id")
                (project_dir / "sections" / f"{safe_sec_id}.json").write_text(
                    json.dumps(sec_state.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            # Sync audit_events.jsonl from authoritative SQLite table
            events = self.get_audit_events(project_id)
            audit_jsonl = project_dir / "logs" / "audit_events.jsonl"
            lines = [json.dumps(ev, ensure_ascii=False) for ev in events]
            audit_jsonl.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except Exception as exc:
            repair_marker = project_dir / "logs" / "snapshot_sync_pending.json"
            try:
                repair_marker.write_text(
                    json.dumps(
                        {
                            "project_id": project_id,
                            "state_version": state.state_version,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass

    def repair_derived_snapshots(self, project_id: str) -> ProjectWorkspaceState:
        """Rebuild all derived disk JSON snapshots from the authoritative SQLite record (T-TXN-05)."""
        state = self.load_state(project_id)
        self.sync_derived_snapshots(state)
        repair_marker = self.get_project_dir(project_id) / "logs" / "snapshot_sync_pending.json"
        if repair_marker.exists():
            repair_marker.unlink()
        return state

    def create_initial_state(
        self,
        state: ProjectWorkspaceState,
        audit_summary: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> ProjectWorkspaceState:
        """Create a new project using strict create-only semantics (R2-P1-04).
        Raises ProjectAlreadyExistsError (`PROJECT_ALREADY_EXISTS`) if `project_id` already exists.
        """
        project_id = validate_safe_identifier(state.brief.project_id, "project_id")
        now_iso = datetime.now(timezone.utc).isoformat()
        state.updated_at = now_iso
        state.state_version = 1
        state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)

        conn = self._connect(autocommit=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT project_id, stage, state_version FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if existing is not None:
                raise ProjectAlreadyExistsError(
                    f"Project '{project_id}' already exists (stage='{existing['stage']}', state_version={existing['state_version']}). "
                    "create_project is create-only; use get_project_status or get_project_context to resume.",
                    details={
                        "project_id": project_id,
                        "existing_stage": existing["stage"],
                        "existing_state_version": int(existing["state_version"]),
                    },
                )
            conn.execute(
                """
                INSERT INTO projects (project_id, stage, updated_at, state_version, state_json)
                VALUES (?, ?, ?, 1, ?)
                """,
                (project_id, state.stage, now_iso, state_json),
            )
            conn.execute(
                """
                INSERT INTO audit_events (project_id, timestamp, event_type, summary, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    now_iso,
                    "create_project",
                    audit_summary,
                    json.dumps(audit_payload or {}, ensure_ascii=False),
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise ProjectAlreadyExistsError(
                f"Project '{project_id}' already exists. create_project is create-only.",
                details={"project_id": project_id},
            ) from exc
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        self.sync_derived_snapshots(state)
        return state

    def transact_project(
        self,
        project_id: str,
        tool_name: str,
        mutator: Callable[[ProjectWorkspaceState, sqlite3.Connection], tuple[dict[str, Any], str, dict[str, Any]]],
        *,
        idempotency_key: str | None = None,
        request_payload: Any | None = None,
        simulate_mid_transaction_crash: bool = False,
    ) -> dict[str, Any]:
        """Execute a read-validate-mutate-persist-audit cycle inside a single SQLite BEGIN IMMEDIATE transaction with CAS."""
        validate_safe_identifier(project_id, "project_id")
        req_hash = compute_canonical_request_hash(request_payload) if request_payload is not None else ""

        conn = self._connect(autocommit=True)
        committed_state: ProjectWorkspaceState | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Check idempotency key inside the serialized transaction
            if idempotency_key:
                idem_row = conn.execute(
                    """
                    SELECT request_hash, response_json
                    FROM idempotency_records
                    WHERE project_id = ? AND idempotency_key = ?
                    """,
                    (project_id, idempotency_key),
                ).fetchone()
                if idem_row is not None:
                    stored_hash = idem_row["request_hash"] or ""
                    if stored_hash and req_hash and stored_hash != req_hash:
                        raise IdempotencyKeyConflictError(
                            f"Idempotency key '{idempotency_key}' was already used with a different request payload.",
                            details={
                                "project_id": project_id,
                                "idempotency_key": idempotency_key,
                                "tool_name": tool_name,
                            },
                        )
                    conn.execute("COMMIT")
                    cached_resp = json.loads(idem_row["response_json"])
                    cached_copy = dict(cached_resp)
                    cached_copy["idempotent_replay"] = True
                    return cached_copy

            # 2. Load current authoritative state and state_version
            row = conn.execute(
                "SELECT state_version, state_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Project '{project_id}' not found in workspace '{self.workspace_root}'.")

            expected_version = int(row["state_version"])
            state = ProjectWorkspaceState.model_validate_json(row["state_json"])
            state.state_version = expected_version

            # 3. Execute domain validation & state mutation
            result_dict, audit_summary, audit_payload = mutator(state, conn)

            # 4. Prepare new state version and CAS update
            now_iso = datetime.now(timezone.utc).isoformat()
            new_version = expected_version + 1
            state.state_version = new_version
            state.updated_at = now_iso
            state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)

            cur = conn.execute(
                """
                UPDATE projects
                SET stage = ?, updated_at = ?, state_version = ?, state_json = ?
                WHERE project_id = ? AND state_version = ?
                """,
                (state.stage, now_iso, new_version, state_json, project_id, expected_version),
            )
            if cur.rowcount != 1:
                raise RevisionConflictError(
                    f"Concurrent state update conflict on project '{project_id}' (expected state_version={expected_version}).",
                    details={"project_id": project_id, "expected_state_version": expected_version},
                )

            if simulate_mid_transaction_crash:
                raise RuntimeError("Simulated mid-transaction crash before idempotency and audit commit.")

            # 5. Record idempotency response in the SAME transaction
            if idempotency_key:
                result_dict["idempotent_replay"] = False
                result_dict["state_version"] = new_version
                conn.execute(
                    """
                    INSERT INTO idempotency_records
                    (project_id, idempotency_key, tool_name, request_hash, result_state_version, created_at, response_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        idempotency_key,
                        tool_name,
                        req_hash,
                        new_version,
                        now_iso,
                        json.dumps(result_dict, ensure_ascii=False),
                    ),
                )

            # 6. Record audit event in the SAME transaction
            conn.execute(
                """
                INSERT INTO audit_events (project_id, timestamp, event_type, summary, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    now_iso,
                    tool_name,
                    audit_summary,
                    json.dumps(audit_payload, ensure_ascii=False),
                ),
            )

            conn.execute("COMMIT")
            committed_state = state
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        if committed_state is not None:
            self.sync_derived_snapshots(committed_state)

        return result_dict

    def save_state(self, state: ProjectWorkspaceState) -> None:
        """Direct save_state helper wrapped in atomic CAS update."""
        project_id = validate_safe_identifier(state.brief.project_id, "project_id")
        now_iso = datetime.now(timezone.utc).isoformat()
        state.updated_at = now_iso

        conn = self._connect(autocommit=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state_version FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                state.state_version = 1
                state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)
                conn.execute(
                    """
                    INSERT INTO projects (project_id, stage, updated_at, state_version, state_json)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (project_id, state.stage, now_iso, state_json),
                )
            else:
                expected_version = int(row["state_version"])
                new_version = expected_version + 1
                state.state_version = new_version
                state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)
                cur = conn.execute(
                    """
                    UPDATE projects
                    SET stage = ?, updated_at = ?, state_version = ?, state_json = ?
                    WHERE project_id = ? AND state_version = ?
                    """,
                    (state.stage, now_iso, new_version, state_json, project_id, expected_version),
                )
                if cur.rowcount != 1:
                    raise RevisionConflictError(
                        f"Concurrent state update conflict on project '{project_id}'."
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        self.sync_derived_snapshots(state)

    def load_state(self, project_id: str) -> ProjectWorkspaceState:
        """Load authoritative state from SQLite."""
        validate_safe_identifier(project_id, "project_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_version, state_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is not None:
            st = ProjectWorkspaceState.model_validate_json(row["state_json"])
            st.state_version = int(row["state_version"])
            return st

        snapshot_path = self.get_project_dir(project_id) / "state_snapshot.json"
        if snapshot_path.exists():
            return ProjectWorkspaceState.model_validate_json(snapshot_path.read_text(encoding="utf-8"))

        raise KeyError(f"Project '{project_id}' not found in workspace '{self.workspace_root}'.")

    # =========================================================================
    # R2-P0-01: Non-Privileged Approval Requests & Trusted Human Confirmation
    # =========================================================================

    @staticmethod
    def create_approval_request_in_tx(
        state: ProjectWorkspaceState,
        artifact_type: str,
        target_id: str,
        artifact_revision: int,
        artifact_hash: str,
        ttl_seconds: int = 3600,
    ) -> ApprovalRequest:
        """Create a non-privileged pending ApprovalRequest in state (never returns any secret token)."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        normalized_hash = (
            artifact_hash if artifact_hash.startswith("sha256:") else f"sha256:{artifact_hash}"
        )

        # Supersede any prior pending request for the same target
        for existing_req in state.approval_requests.values():
            if (
                existing_req.artifact_type == artifact_type
                and existing_req.target_id == target_id
                and existing_req.status == "pending"
            ):
                existing_req.status = "stale"

        req_id = f"aprq_{secrets.token_hex(8)}"
        req = ApprovalRequest(
            approval_request_id=req_id,
            project_id=state.brief.project_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]
            target_id=target_id,
            artifact_revision=artifact_revision,
            artifact_hash=normalized_hash,
            status="pending",
            requires_human_action=True,
            created_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )
        state.approval_requests[req_id] = req
        return req

    @staticmethod
    def decide_approval_request_in_tx(
        state: ProjectWorkspaceState,
        approval_request_id: str,
        decision: str,
        human_actor_id: str,
        confirmation_channel: str,
        current_revision: int,
        current_hash: str,
        comment: str = "",
    ) -> tuple[ApprovalRequest, ApprovalDecision]:
        """Record a human approval/rejection decision inside the trusted confirmation domain (R2-P0-01)."""
        if confirmation_channel not in {
            "trusted_human_portal",
            "trusted_local_ui",
            "trusted_host_callback",
        }:
            raise UntrustedApprovalChannelError(
                f"Channel '{confirmation_channel}' is not a trusted human confirmation channel.",
                details={"confirmation_channel": confirmation_channel},
            )
        normalized_actor = (human_actor_id or "").strip().lower()
        if not normalized_actor or normalized_actor in {"agent", "mcp", "mcp_agent", "cli_agent", "ai", "system", "auto"}:
            raise UntrustedApprovalChannelError(
                f"Actor '{human_actor_id}' is not a valid human actor identity.",
                details={"human_actor_id": human_actor_id},
            )

        req = state.approval_requests.get(approval_request_id)
        if req is None:
            raise ApprovalConflictError(
                f"ApprovalRequest '{approval_request_id}' not found.",
                code="APPROVAL_REQUIRED",
                details={"approval_request_id": approval_request_id},
            )
        if req.status != "pending":
            raise ApprovalConflictError(
                f"ApprovalRequest '{approval_request_id}' is no longer pending (current status='{req.status}').",
                code="APPROVAL_STALE",
                details={"approval_request_id": approval_request_id, "status": req.status},
            )

        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(req.expires_at)
        if now > expires:
            req.status = "expired"
            raise ApprovalConflictError(
                f"ApprovalRequest '{approval_request_id}' expired at {req.expires_at}.",
                code="APPROVAL_STALE",
                details={"approval_request_id": approval_request_id, "expires_at": req.expires_at},
            )

        normalized_current_hash = (
            current_hash if current_hash.startswith("sha256:") else f"sha256:{current_hash}"
        )
        if req.artifact_revision != current_revision or req.artifact_hash != normalized_current_hash:
            req.status = "stale"
            raise ApprovalConflictError(
                f"ApprovalRequest '{approval_request_id}' was requested for revision {req.artifact_revision} "
                f"({req.artifact_hash[:16]}), but artifact is now at revision {current_revision} ({normalized_current_hash[:16]}).",
                code="APPROVAL_STALE",
                details={
                    "approval_request_id": approval_request_id,
                    "request_revision": req.artifact_revision,
                    "current_revision": current_revision,
                    "request_hash": req.artifact_hash,
                    "current_hash": normalized_current_hash,
                },
            )

        if decision not in {"approved", "rejected"}:
            raise ApprovalConflictError(
                f"Invalid approval decision '{decision}'; must be 'approved' or 'rejected'.",
                details={"decision": decision},
            )

        audit_id = f"aud_apr_{secrets.token_hex(6)}"
        decision_record = ApprovalDecision(
            request_id=approval_request_id,
            decision=decision,  # type: ignore[arg-type]
            human_actor_id=human_actor_id.strip(),
            confirmation_channel=confirmation_channel,  # type: ignore[arg-type]
            confirmed_at=now.isoformat(),
            artifact_revision=current_revision,
            artifact_hash=normalized_current_hash,
            audit_id=audit_id,
            comment=comment,
        )
        req.status = decision  # type: ignore[assignment]
        req.decision = decision_record
        return req, decision_record

    # =========================================================================
    # Internal Hashed One-Time Tokens (Trusted Domain / Legacy Test Harness Only)
    # =========================================================================

    def issue_approval_token(
        self,
        project_id: str,
        artifact_type: str,
        target_id: str,
        approved_revision: int,
        content_hash: str,
        channel: str = "local_confirmation_token",
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Internal helper for trusted confirmation domain or explicit test harness.
        Stores ONLY the SHA-256 hash of the token in SQLite and NEVER writes the token to disk (R2-P0-01 §1.3C).
        """
        validate_safe_identifier(project_id, "project_id")
        if channel == "test_harness_token" and os.environ.get("DAF_ALLOW_TEST_HARNESS_TOKENS", "1") != "1":
            raise UntrustedApprovalChannelError(
                "Channel 'test_harness_token' is disabled outside test environments."
            )

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        token_prefix = "appr_test_" if channel == "test_harness_token" else "appr_usr_"
        token = f"{token_prefix}{secrets.token_hex(16)}"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        conn = self._connect(autocommit=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO approval_tokens
                (token_hash, project_id, artifact_type, target_id, approved_revision, content_hash, channel, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    token_hash,
                    project_id,
                    artifact_type,
                    target_id,
                    approved_revision,
                    content_hash,
                    channel,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return {
            "confirmation_token": token,
            "project_id": project_id,
            "artifact_type": artifact_type,
            "target_id": target_id,
            "approved_revision": approved_revision,
            "content_hash": content_hash,
            "channel": channel,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }

    def consume_approval_token_in_tx(
        self,
        conn: sqlite3.Connection,
        token: str,
        project_id: str,
        artifact_type: str,
        target_id: str,
        expected_revision: int,
        expected_hash: str,
    ) -> str:
        """Verify and consume a hashed one-time approval token inside an active SQLite transaction."""
        token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
        row = conn.execute(
            """
            SELECT token_hash, project_id, artifact_type, target_id, approved_revision, content_hash, channel, expires_at, used_at
            FROM approval_tokens
            WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            raise UntrustedApprovalChannelError(
                "Invalid or unknown confirmation_token.",
                details={"project_id": project_id, "artifact_type": artifact_type},
            )
        if row["used_at"] is not None:
            raise UntrustedApprovalChannelError(
                "Confirmation token has already been used (one-time use only).",
                details={"project_id": project_id, "artifact_type": artifact_type},
            )
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(row["expires_at"])
        if now > expires:
            raise ApprovalConflictError(
                "Confirmation token has expired.",
                code="APPROVAL_STALE",
                details={"project_id": project_id, "artifact_type": artifact_type},
            )
        if (
            row["project_id"] != project_id
            or row["artifact_type"] != artifact_type
            or row["target_id"] != target_id
        ):
            raise UntrustedApprovalChannelError(
                "Confirmation token scope mismatch (project_id, artifact_type, or target_id does not match).",
                details={"project_id": project_id, "artifact_type": artifact_type, "target_id": target_id},
            )
        if int(row["approved_revision"]) != expected_revision or row["content_hash"] != expected_hash:
            raise ApprovalConflictError(
                f"Confirmation token was issued for revision {row['approved_revision']} (hash {row['content_hash'][:10]}), "
                f"but current artifact is at revision {expected_revision} (hash {expected_hash[:10]}).",
                code="APPROVAL_STALE",
                details={
                    "token_revision": int(row["approved_revision"]),
                    "current_revision": expected_revision,
                    "token_hash": row["content_hash"],
                    "current_hash": expected_hash,
                },
            )

        conn.execute(
            "UPDATE approval_tokens SET used_at = ? WHERE token_hash = ?",
            (now.isoformat(), token_hash),
        )
        return str(row["channel"])

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, stage, state_version, updated_at FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_audit_event(
        self,
        project_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        validate_safe_identifier(project_id, "project_id")
        now_iso = datetime.now(timezone.utc).isoformat()
        payload_data = payload or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (project_id, timestamp, event_type, summary, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    now_iso,
                    event_type,
                    summary,
                    json.dumps(payload_data, ensure_ascii=False),
                ),
            )
            conn.commit()

        project_dir = self.ensure_project_dirs(project_id)
        audit_jsonl = project_dir / "logs" / "audit_events.jsonl"
        record = {
            "timestamp": now_iso,
            "project_id": project_id,
            "event_type": event_type,
            "summary": summary,
            "payload": payload_data,
        }
        with audit_jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_audit_events(self, project_id: str) -> list[dict[str, Any]]:
        validate_safe_identifier(project_id, "project_id")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, event_type, summary, payload_json
                FROM audit_events
                WHERE project_id = ?
                ORDER BY id ASC
                """,
                (project_id,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for r in rows:
            events.append(
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "event_type": r["event_type"],
                    "summary": r["summary"],
                    "payload": json.loads(r["payload_json"]),
                }
            )
        return events
