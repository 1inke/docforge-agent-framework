# DAF Architecture (Local Single-User Edition)

## 1. Threat Model & Design Principles

DAF is a **local single-user writing guardrail** for AI Agents producing evidence-grounded professional Word documents.

- **Scope**: One local user, one machine, same OS user for both Agent and DAF.
- **Threat model**: **Prevent Agent carelessness and hallucination, not a malicious OS-level attacker.**
- **Four core invariants**:
  1. **Guardrails over security theater**: Keep low-cost checks that block Agent mistakes (`DAF_ALLOWED_SOURCE_ROOTS`, `call_agent_tool` allowlist, safe output filenames, CAS + idempotency, `CONTEXT_STALE`).
  2. **Prose is the sole source of claims**: Inline Pandoc citations (`[@ev_01]`, `[@ev_01; @ev_02]`, `[@hypothesis]`) are parsed from `content_markdown` via Pandoc JSON AST. Self-reported claim lists are not accepted.
  3. **User owns constraints**: Document profiles (`<workspace>/profiles/<id>.yaml`), user constraints, forbidden scopes, L3 reliability (`trusted`), and outline/final approvals are controlled exclusively by the local user via `daf ui` (127.0.0.1 with one-time token) or `daf` CLI.
  4. **Read-only queries, O(1) metadata writes**: Read tools never mutate `state_version` or write audit rows. Large immutable texts live in content-addressed SQLite `blobs` so write latency does not scale with source corpus size.

## 2. Module Layout

| Module | Responsibility |
| --- | --- |
| `src/daf/core/pandoc_ast.py` | Pandoc JSON AST parsing, sentence splitting, inline citation extraction, and L2a checks (R-A..R-D). |
| `src/daf/core/source_import.py` | Default-deny `DAF_ALLOWED_SOURCE_ROOTS` path boundary validation. |
| `src/daf/core/engine.py` | Unified orchestration facade (`DAFEngine`), Agent allowlist (`call_agent_tool`), and local user APIs. |
| `src/daf/project_store/store.py` | Split-table SQLite store (`projects`, `blobs`, `source_slices`, `section_revisions`, `idempotency_records`, `audit_events`) with `BEGIN IMMEDIATE` + CAS. |
| `src/daf/document_profiles/profiles.py` | Neutral `default` profile + user-authored YAML profile loader (`<workspace>/profiles/*.yaml`). |
| `src/daf/context_manager/manager.py` | Source slicing, windowed reading, character-budgeted context assembly, `context_fingerprint`, and L1/L2a/L2b checks. |
| `src/daf/workflow_engine/state_machine.py` | Outline/section revisioning, inline-citation binding, approval invalidation, and release readiness guards. |
| `src/daf/review_manager/reviewer.py` | Independent semantic review (`REVIEWER_IS_AUTHOR`), quote-anchored checklists, and submit-is-accept transitions. |
| `src/daf/guidance/` | Read-only `TaskGuidanceBuilder` and `state_resolver` (single source of truth for `next_action`). |
| `src/daf/document_engine/` | Single-pass Pandoc DOCX renderer (`--reference-doc`, `--citeproc`, GB/T 7714 CSL, Lua crossref filter, OMML) + mapped template insertion. |
| `src/daf/interfaces/` | Streamlined MCP server (`daf-mcp`), localhost-only approval UI (`daf ui`), CLI (`daf`), and HTTP API. |

## 3. SQLite Storage Model

| Table | Mutability | Contents |
| --- | --- | --- |
| `projects` | Mutable (CAS) | Compact JSON metadata: stage, outline, approvals, review tasks, pointers (`sha256`). |
| `blobs` | Immutable | Content-addressed (`sha256`) source slice raw text and section revision markdown. |
| `source_slices` | Immutable | Slice index (`project_id`, `slice_id`, `source_id`, `locator`, `char_count`, `blob_sha256`). |
| `section_revisions` | Append-only | Section revision metadata (`project_id`, `section_id`, `revision`, `blob_sha256`, `author_session`). |
| `idempotency_records` | Append-only | Required `request_hash` + cached response per `(project_id, idempotency_key)`. |
| `audit_events` | Append-only | Structured audit trail for state-mutating operations. |
