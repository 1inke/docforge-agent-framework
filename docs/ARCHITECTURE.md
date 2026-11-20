# DAF Architecture (Local Single-User Edition)

## 1. Threat Model & Design Principles

DAF is a **local single-user writing guardrail** for AI Agents producing evidence-grounded professional Word documents.

- **Scope**: One local user, one machine, same OS user for both Agent and DAF.
- **Threat model**: **Prevent Agent carelessness and hallucination, not a malicious OS-level attacker.**
- **Four core invariants**:
  1. **Guardrails over security theater**: Keep low-cost checks that block Agent mistakes (`DAF_ALLOWED_SOURCE_ROOTS`, `call_agent_tool` allowlist, safe output filenames, CAS + idempotency, `CONTEXT_STALE`, `SECTION_NOT_IN_OUTLINE`).
  2. **Prose is the sole source of claims**: Inline Pandoc citations (`[@ev_01]`, `[@ev_01; @ev_02]`, `[@hypothesis]`) and cross-references (`@tbl:`, `@fig:`, `@eq:`) are parsed from `content_markdown` via Pandoc JSON AST. Self-reported claim lists are not accepted, and unconfirmed source bibliographies / unresolved cross-references block release.
  3. **User owns constraints & bibliography confirmation**: Document profiles (`<workspace>/profiles/<id>.yaml`), user constraints, forbidden scopes, L3 reliability (`trusted`), bibliography confirmation (`daf sources set-bib` / Local UI), and outline/final approvals are controlled exclusively by the local user via `daf ui` (127.0.0.1 with one-time token) or `daf` CLI.
  4. **Read-only queries, O(1) metadata writes, transactional blob integrity**: Read tools never mutate `state_version` or write audit rows. Source slice payloads live exclusively in `blobs` + indexed `source_slices` (`idx_source_slices_proj_src`, omitted from `state_json` and lazy-hydrated on demand), inline `SectionState.history` in `state_json` is capped at the latest 5 revisions (full revisions remain in `blobs` + `section_revisions`), `_BlobLRUCache` (64 MB cap) populates strictly post-commit so rollbacks cannot poison memory, and missing blobs fail closed with `STATE_CORRUPTED` (diagnosable via `daf doctor`).

## 2. Module Layout

| Module | Responsibility |
| --- | --- |
| `src/daf/core/pandoc_ast.py` | Pandoc JSON AST parsing, sentence splitting, inline citation/crossref extraction, narrowed R-A (`major`/`minor` + `extra_physical_units`) and R-B..R-D checks. |
| `src/daf/core/source_import.py` | Default-deny `DAF_ALLOWED_SOURCE_ROOTS` path boundary validation. |
| `src/daf/core/engine.py` | Unified orchestration facade (`DAFEngine`), Agent allowlist (`call_agent_tool`), bibliography proposal/confirmation workflows, and local user APIs. |
| `src/daf/project_store/store.py` | Split-table SQLite store (`projects`, `blobs`, `source_slices`, `section_revisions`, `idempotency_records`, `audit_events`) with `BEGIN IMMEDIATE` + CAS, post-commit 64 MB `_BlobLRUCache`, fail-closed `STATE_CORRUPTED`, and `doctor_project`. |
| `src/daf/document_profiles/profiles.py` | Neutral `default` profile + user-authored YAML profile loader (`<workspace>/profiles/*.yaml`). |
| `src/daf/context_manager/manager.py` | Source slicing, O(1) SQLite-backed slice listing/search, windowed reading, character-budgeted context assembly, `context_fingerprint`, and L1/L2a/L2b checks. |
| `src/daf/workflow_engine/state_machine.py` | Outline/section revisioning (`SECTION_NOT_IN_OUTLINE`), inline-citation binding, approval invalidation, and release readiness guards (`BIBLIOGRAPHY_MISSING`, `CROSSREF_UNRESOLVED`). |
| `src/daf/review_manager/reviewer.py` | Independent semantic review (`REVIEWER_ID_REQUIRED`, `REVIEWER_IS_AUTHOR`), quote-anchored checklists, `UNCITED_FACTUAL_CLAIM` issue retention, and submit-is-accept transitions. |
| `src/daf/guidance/` | Read-only `TaskGuidanceBuilder` and `state_resolver` (single source of truth for `next_action`). |
| `src/daf/document_engine/` | Unified single-pass Pandoc DOCX renderer (`builtin` & `mapped` via sentinel headings, custom `LayoutProfile` `reference.docx`, official Zotero `gb-t-7714-2015-numeric.csl`, two-pass `crossref.lua`, OMML). Legacy `ir.py` and `tagged` template mode have been removed. |
| `src/daf/interfaces/` | Streamlined MCP server (`daf-mcp`, 18 tools), localhost-only approval & bibliography UI (`daf ui`), CLI (`daf`, including `sources set-bib` and `doctor`), and HTTP API (mapped HTTP 403/409/500 responses). |

## 3. SQLite Storage Model

| Table | Mutability | Contents |
| --- | --- | --- |
| `projects` | Mutable (CAS) | Compact JSON metadata: stage, outline, approvals, review tasks, capped inline section history (`<=5`), slice counts, and blob pointers (`sha256`). |
| `blobs` | Immutable | Content-addressed (`sha256`) source slice raw text and full section revision markdown. |
| `source_slices` | Immutable | Indexed slice catalog (`idx_source_slices_proj_src` on `(project_id, source_id, slice_id)`) linking `slice_id` to `blob_sha256` with `locator` and `char_count`. |
| `section_revisions` | Append-only | Full section revision metadata (`project_id`, `section_id`, `revision`, `blob_sha256`, `author_session`). |
| `idempotency_records` | Append-only | Required `request_hash` + cached response per `(project_id, idempotency_key)`. |
| `audit_events` | Append-only | Structured audit trail for state-mutating operations. |
