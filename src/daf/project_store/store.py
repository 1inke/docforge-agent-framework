"""Project Workspace & SQLite Atomic CAS State Store (split-table blob model)."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
    ProjectNotFoundError,
    RevisionConflictError,
    SecurityBoundaryError,
    StateCorruptedError,
    UntrustedApprovalChannelError,
)

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")
SAFE_DOCX_FILENAME_PATTERN = re.compile(
    r"^[A-Za-z0-9_\-\u4e00-\u9fff][A-Za-z0-9_\-. \u4e00-\u9fff]{0,120}\.docx$",
    re.IGNORECASE,
)
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
    "figures",
    "logs",
)

DB_SCHEMA_VERSION = 4


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
    """Validate that `output_filename` is a single safe `.docx` filename with no traversal or reserved names."""
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


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


MAX_BLOB_CACHE_BYTES = 64 * 1024 * 1024


class _BlobLRUCache:
    """Bounded UTF-8 byte-size LRU cache for immutable committed content blobs."""

    def __init__(self, max_bytes: int = MAX_BLOB_CACHE_BYTES) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._total_bytes = 0

    def get(self, sha: str) -> str | None:
        entry = self._items.get(sha)
        if entry is None:
            return None
        self._items.move_to_end(sha)
        return entry[0]

    def put(self, sha: str, content: str) -> None:
        size = len(content.encode("utf-8"))
        if size > self.max_bytes:
            return
        if sha in self._items:
            old_size = self._items.pop(sha)[1]
            self._total_bytes -= old_size
        while self._total_bytes + size > self.max_bytes and self._items:
            _k, (_v, evicted_size) = self._items.popitem(last=False)
            self._total_bytes -= evicted_size
        self._items[sha] = (content, size)
        self._total_bytes += size

    def put_many(self, mapping: dict[str, str]) -> None:
        for sha, content in mapping.items():
            self.put(sha, content)


class ProjectStore:
    """SQLite authoritative state store with content-addressed immutable blobs and CAS transactions."""

    def __init__(self, workspace_root: str | Path) -> None:
        raw_root = Path(workspace_root)
        if raw_root.exists() and raw_root.is_symlink():
            raise SecurityBoundaryError(
                f"Security violation: workspace_root '{raw_root}' must not be a symlink."
            )
        self.workspace_root = raw_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace_root / "daf_workspace.sqlite3"
        self._blob_cache = _BlobLRUCache()
        self._init_db()

    def _connect(self, autocommit: bool = False, readonly: bool = False) -> sqlite3.Connection:
        if readonly and self.db_path.exists():
            uri = f"{self.db_path.as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30.0)
            conn.row_factory = sqlite3.Row
            return conn
        isolation = None if autocommit else ""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=isolation)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=15000;")
        return conn

    def _init_db(self) -> None:
        conn = self._connect(autocommit=True)
        try:
            ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if ver >= DB_SCHEMA_VERSION:
                return
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
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_slices (
                    project_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    blob_sha256 TEXT NOT NULL,
                    PRIMARY KEY (project_id, slice_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS section_revisions (
                    project_id TEXT NOT NULL,
                    section_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    blob_sha256 TEXT NOT NULL,
                    author_session TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, section_id, revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    project_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
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
            conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_project_dir(self, project_id: str) -> Path:
        safe_id = validate_safe_identifier(project_id, "project_id")
        candidate = (self.workspace_root / "projects" / safe_id).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise SecurityBoundaryError(
                f"Security violation: project path '{candidate}' escapes workspace '{self.workspace_root}'."
            ) from exc
        return candidate

    def ensure_project_dirs(self, project_id: str) -> Path:
        project_dir = self.get_project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        for sub in WORKSPACE_SUBDIRS:
            sub_dir = project_dir / sub
            if sub == "artifacts" and sub_dir.exists() and sub_dir.is_symlink():
                raise ArtifactPathInvalidError(
                    f"Security violation: artifacts directory '{sub_dir}' must not be a symlink."
                )
            sub_dir.mkdir(parents=True, exist_ok=True)
        return project_dir

    # =========================================================================
    # Content-Addressed Blob Extraction & Hydration
    # =========================================================================

    def _put_blob_in_tx(
        self,
        conn: sqlite3.Connection,
        sha: str,
        content: str,
        now_iso: str,
        pending_blobs: dict[str, str],
    ) -> None:
        # Always issue idempotent SQLite INSERT; never skip based on in-process cache
        conn.execute(
            "INSERT OR IGNORE INTO blobs (sha256, content, created_at) VALUES (?, ?, ?)",
            (sha, content, now_iso),
        )
        pending_blobs[sha] = content

    def _compact_state_and_store_blobs(
        self,
        state: ProjectWorkspaceState,
        conn: sqlite3.Connection,
        now_iso: str,
        pending_blobs: dict[str, str],
    ) -> str:
        """Store immutable slice/section text blobs separately and return compact metadata-only JSON."""
        project_id = state.brief.project_id
        state_dict = state.model_dump(mode="json")

        # 1. Offload source slices
        slices_dict = state_dict.get("source_registry", {}).get("slices", {})
        for slice_id, sl in slices_dict.items():
            raw_text = sl.get("raw_text") or ""
            sha = sl.get("sha256") or _sha256_text(raw_text)
            sl["sha256"] = sha
            if raw_text:
                self._put_blob_in_tx(conn, sha, raw_text, now_iso, pending_blobs)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO source_slices
                    (project_id, slice_id, source_id, locator, char_count, blob_sha256)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        slice_id,
                        sl.get("source_id", ""),
                        sl.get("locator", ""),
                        int(sl.get("char_count", len(raw_text))),
                        sha,
                    ),
                )
            sl["raw_text"] = ""
            sl["normalized_text"] = ""

        # 2. Offload section current & historical markdown
        sections_dict = state_dict.get("section_versions", {}).get("sections", {})
        for sec_id, sec in sections_dict.items():
            cur_md = sec.get("content_markdown") or ""
            if cur_md:
                chash = sec.get("content_hash") or _sha256_text(cur_md)
                sec["content_hash"] = chash
                self._put_blob_in_tx(conn, chash, cur_md, now_iso, pending_blobs)
            sec["content_markdown"] = ""

            for hist in sec.get("history") or []:
                h_md = hist.get("content_markdown") or ""
                h_hash = hist.get("content_hash") or (_sha256_text(h_md) if h_md else "")
                if h_md and h_hash:
                    hist["content_hash"] = h_hash
                    self._put_blob_in_tx(conn, h_hash, h_md, now_iso, pending_blobs)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO section_revisions
                        (project_id, section_id, revision, blob_sha256, author_session, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            sec_id,
                            int(hist.get("revision", 0)),
                            h_hash,
                            hist.get("author_session", ""),
                            hist.get("created_at", now_iso),
                        ),
                    )
                hist["content_markdown"] = ""

        return json.dumps(state_dict, ensure_ascii=False, separators=(",", ":"))

    def _fetch_blobs(self, conn: sqlite3.Connection, shas: set[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        missing: list[str] = []
        for sha in shas:
            if not sha:
                continue
            cached = self._blob_cache.get(sha)
            if cached is not None:
                result[sha] = cached
            else:
                missing.append(sha)
        if missing:
            for i in range(0, len(missing), 500):
                chunk = missing[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT sha256, content FROM blobs WHERE sha256 IN ({placeholders})",
                    chunk,
                ).fetchall()
                for r in rows:
                    s_val = str(r["sha256"])
                    c_val = str(r["content"])
                    self._blob_cache.put(s_val, c_val)
                    result[s_val] = c_val
        return result

    def _hydrate_state_dict(
        self,
        state_dict: dict[str, Any],
        conn: sqlite3.Connection,
        *,
        hydrate_slices: bool = True,
        hydrate_sections: bool = True,
    ) -> ProjectWorkspaceState:
        needed: set[str] = set()
        slices_dict = state_dict.get("source_registry", {}).get("slices", {})
        sections_dict = state_dict.get("section_versions", {}).get("sections", {})

        if hydrate_slices:
            for sl in slices_dict.values():
                if not sl.get("raw_text") and sl.get("sha256"):
                    needed.add(sl["sha256"])
        if hydrate_sections:
            for sec in sections_dict.values():
                if not sec.get("content_markdown") and sec.get("content_hash"):
                    needed.add(sec["content_hash"])
                for hist in sec.get("history") or []:
                    if not hist.get("content_markdown") and hist.get("content_hash"):
                        needed.add(hist["content_hash"])

        blobs = self._fetch_blobs(conn, needed) if needed else {}
        missing_refs: list[dict[str, str]] = []

        if hydrate_slices:
            for slice_id, sl in slices_dict.items():
                sha = sl.get("sha256")
                if not sl.get("raw_text") and sha:
                    if sha in blobs:
                        sl["raw_text"] = blobs[sha]
                    else:
                        missing_refs.append({"owner": f"slice:{slice_id}", "sha256": sha})
        if hydrate_sections:
            for sec_id, sec in sections_dict.items():
                chash = sec.get("content_hash")
                if not sec.get("content_markdown") and chash:
                    if chash in blobs:
                        sec["content_markdown"] = blobs[chash]
                    else:
                        missing_refs.append({"owner": f"section:{sec_id}@r{sec.get('revision', 0)}", "sha256": chash})
                for hist in sec.get("history") or []:
                    hhash = hist.get("content_hash")
                    if not hist.get("content_markdown") and hhash:
                        if hhash in blobs:
                            hist["content_markdown"] = blobs[hhash]
                        else:
                            missing_refs.append({"owner": f"section_history:{sec_id}@r{hist.get('revision', 0)}", "sha256": hhash})

        if missing_refs:
            raise StateCorruptedError(
                f"SQLite state references {len(missing_refs)} missing blob(s): {missing_refs[:5]}",
                details={"missing_blobs": missing_refs},
            )

        return ProjectWorkspaceState.model_validate(state_dict)

    # =========================================================================
    # On-Demand Snapshot Export (replaces automatic per-write snapshot rewrite)
    # =========================================================================

    def export_snapshot(self, project_id: str) -> Path:
        """Export a human-readable JSON snapshot of the project on demand (CLI: daf export-snapshot)."""
        state = self.load_state(project_id)
        project_dir = self.ensure_project_dirs(project_id)
        snap_path = project_dir / "state_snapshot.json"
        snap_path.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return snap_path

    # =========================================================================
    # Atomic Transactions & Read-Only Queries
    # =========================================================================

    def create_initial_state(
        self,
        state: ProjectWorkspaceState,
        audit_summary: str,
        audit_payload: dict[str, Any] | None = None,
    ) -> ProjectWorkspaceState:
        """Create a new project using strict create-only semantics."""
        project_id = validate_safe_identifier(state.brief.project_id, "project_id")
        now_iso = datetime.now(timezone.utc).isoformat()
        state.updated_at = now_iso
        state.state_version = 1

        conn = self._connect(autocommit=True)
        pending_blobs: dict[str, str] = {}
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
            compact_json = self._compact_state_and_store_blobs(state, conn, now_iso, pending_blobs)
            conn.execute(
                """
                INSERT INTO projects (project_id, stage, updated_at, state_version, state_json)
                VALUES (?, ?, ?, 1, ?)
                """,
                (project_id, state.stage, now_iso, compact_json),
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
            self._blob_cache.put_many(pending_blobs)
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
        hydrate_slices: bool = True,
        hydrate_sections: bool = True,
    ) -> dict[str, Any]:
        """Execute a read-validate-mutate-persist-audit cycle inside a single SQLite BEGIN IMMEDIATE transaction with CAS."""
        validate_safe_identifier(project_id, "project_id")
        req_hash = (
            compute_canonical_request_hash(
                request_payload if request_payload is not None else {"tool": tool_name, "project_id": project_id}
            )
            if idempotency_key
            else ""
        )

        conn = self._connect(autocommit=True)
        pending_blobs: dict[str, str] = {}
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Check idempotency key inside the serialized transaction (request_hash mandatory)
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
                    if not stored_hash or stored_hash != req_hash:
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
                raise ProjectNotFoundError(
                    f"Project '{project_id}' not found in workspace '{self.workspace_root}'.",
                    details={"project_id": project_id},
                )

            expected_version = int(row["state_version"])
            state = self._hydrate_state_dict(
                json.loads(row["state_json"]),
                conn,
                hydrate_slices=hydrate_slices,
                hydrate_sections=hydrate_sections,
            )
            state.state_version = expected_version

            # 3. Execute domain validation & state mutation
            result_dict, audit_summary, audit_payload = mutator(state, conn)

            # 4. Prepare new state version, offload blobs, and CAS update
            now_iso = datetime.now(timezone.utc).isoformat()
            new_version = expected_version + 1
            state.state_version = new_version
            state.updated_at = now_iso
            compact_json = self._compact_state_and_store_blobs(state, conn, now_iso, pending_blobs)

            cur = conn.execute(
                """
                UPDATE projects
                SET stage = ?, updated_at = ?, state_version = ?, state_json = ?
                WHERE project_id = ? AND state_version = ?
                """,
                (state.stage, now_iso, new_version, compact_json, project_id, expected_version),
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
            self._blob_cache.put_many(pending_blobs)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        return result_dict

    def load_state(
        self,
        project_id: str,
        *,
        hydrate_slices: bool = True,
        hydrate_sections: bool = True,
    ) -> ProjectWorkspaceState:
        """Read-only load of authoritative state from SQLite (never resurrects from JSON snapshots)."""
        validate_safe_identifier(project_id, "project_id")
        if not self.db_path.exists():
            raise ProjectNotFoundError(
                f"Project '{project_id}' not found (database does not exist).",
                details={"project_id": project_id},
            )
        with closing(self._connect(readonly=True)) as conn:
            row = conn.execute(
                "SELECT state_version, state_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(
                    f"Project '{project_id}' not found in workspace '{self.workspace_root}'.",
                    details={"project_id": project_id},
                )
            st = self._hydrate_state_dict(
                json.loads(row["state_json"]),
                conn,
                hydrate_slices=hydrate_slices,
                hydrate_sections=hydrate_sections,
            )
            st.state_version = int(row["state_version"])
            return st

    def doctor_project(self, project_id: str) -> dict[str, Any]:
        """Verify that every blob sha referenced by projects/source_slices/section_revisions exists in blobs."""
        validate_safe_identifier(project_id, "project_id")
        if not self.db_path.exists():
            raise ProjectNotFoundError(
                f"Project '{project_id}' not found (database does not exist).",
                details={"project_id": project_id},
            )
        with closing(self._connect(readonly=True)) as conn:
            row = conn.execute(
                "SELECT state_version, state_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(
                    f"Project '{project_id}' not found in workspace '{self.workspace_root}'.",
                    details={"project_id": project_id},
                )
            state_dict = json.loads(row["state_json"])
            refs: list[tuple[str, str]] = []
            for slice_id, sl in state_dict.get("source_registry", {}).get("slices", {}).items():
                if sl.get("sha256"):
                    refs.append((f"state_slice:{slice_id}", sl["sha256"]))
            for sec_id, sec in state_dict.get("section_versions", {}).get("sections", {}).items():
                if sec.get("content_hash"):
                    refs.append((f"state_section:{sec_id}@r{sec.get('revision', 0)}", sec["content_hash"]))
                for hist in sec.get("history") or []:
                    if hist.get("content_hash"):
                        refs.append((f"state_history:{sec_id}@r{hist.get('revision', 0)}", hist["content_hash"]))
            for sr in conn.execute(
                "SELECT slice_id, blob_sha256 FROM source_slices WHERE project_id = ?",
                (project_id,),
            ).fetchall():
                refs.append((f"table_slice:{sr['slice_id']}", sr["blob_sha256"]))
            for rr in conn.execute(
                "SELECT section_id, revision, blob_sha256 FROM section_revisions WHERE project_id = ?",
                (project_id,),
            ).fetchall():
                refs.append((f"table_revision:{rr['section_id']}@r{rr['revision']}", rr["blob_sha256"]))

            all_shas = {sha for _owner, sha in refs if sha}
            present: set[str] = set()
            sha_list = sorted(all_shas)
            for i in range(0, len(sha_list), 500):
                chunk = sha_list[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                for br in conn.execute(
                    f"SELECT sha256 FROM blobs WHERE sha256 IN ({placeholders})",
                    chunk,
                ).fetchall():
                    present.add(str(br["sha256"]))

            missing = [
                {"owner": owner, "sha256": sha}
                for owner, sha in refs
                if sha and sha not in present
            ]
        return {
            "project_id": project_id,
            "ok": len(missing) == 0,
            "checked_refs": len(refs),
            "unique_blobs": len(all_shas),
            "missing_blobs": missing,
        }

    # =========================================================================
    # Approval Requests & Local User Decisions
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
        """Create or deduplicate a pending ApprovalRequest bound to (artifact_type, target_id, artifact_hash)."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        normalized_hash = (
            artifact_hash if artifact_hash.startswith("sha256:") else f"sha256:{artifact_hash}"
        )

        # Deduplicate pending request with identical (artifact_type, target_id, artifact_hash)
        for existing_req in state.approval_requests.values():
            if (
                existing_req.artifact_type == artifact_type
                and existing_req.target_id == target_id
                and existing_req.status == "pending"
            ):
                if (
                    existing_req.artifact_hash == normalized_hash
                    and existing_req.artifact_revision == artifact_revision
                    and datetime.fromisoformat(existing_req.expires_at) > now
                ):
                    return existing_req
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
        """Record a local user approval/rejection decision."""
        if confirmation_channel not in {"local_ui", "local_cli"}:
            raise UntrustedApprovalChannelError(
                f"Channel '{confirmation_channel}' is not a valid local user confirmation channel.",
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

    def list_projects(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with closing(self._connect(readonly=True)) as conn:
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
        with closing(self._connect()) as conn:
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

    def get_audit_events(self, project_id: str) -> list[dict[str, Any]]:
        validate_safe_identifier(project_id, "project_id")
        if not self.db_path.exists():
            return []
        with closing(self._connect(readonly=True)) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, event_type, summary, payload_json
                FROM audit_events
                WHERE project_id = ?
                ORDER BY id ASC
                """,
                (project_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "event_type": r["event_type"],
                "summary": r["summary"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]
