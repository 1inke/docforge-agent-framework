# DAF Agent Protocol (v2.0)

## 1. Streamlined MCP Tool Surface (18 Tools)

| Group | Tools |
| --- | --- |
| Discovery | `get_capabilities` (compact summary < 3000 chars; pass `include_schemas` on demand), `get_task_guidance` |
| Project | `create_project`, `get_project_status` |
| Sources | `ingest_sources`, `find_sources` (list or literal search), `read_source_block`, `register_evidence`, `propose_source_bibliography` |
| Writing | `submit_outline`, `get_section_context` (with optional `include_content_window`), `submit_section`, `record_agent_note` |
| Review | `start_review` (QA + create & claim tasks), `submit_review` (structured quotes, submit-is-accept) |
| Approval & Release | `request_approval` (supports `query_only=True`), `render_document` (`builtin` / `mapped`), `export_document` |

All MCP tools return structured `DAFError` JSON on failure and write tools include `next_action`.

## 2. Inline Citation, Crossref & Bibliography Governance

- Cite registered evidence inline using Pandoc syntax: `Latency dropped to 9.2 ms[@ev_01]` or `[@ev_01; @ev_02]`.
- Mark unverified research hypotheses inline: `We hypothesize linear scaling[@hypothesis]`.
- Use Pandoc cross-reference syntax for figures (`![caption](path){#fig:id}`), tables (fenced div `::: {#tbl:id} ... :::`), and display equations (`[$$ ... $$]{#eq:id}`), referenced via `@fig:id`, `@tbl:id`, `@eq:id`.
- `submit_section` requires a non-whitespace `author_session` (`>= 3` chars), enforces outline membership once the outline is approved (`SECTION_NOT_IN_OUTLINE`), parses `content_markdown` via `pandoc -f markdown -t json`, and checks:
  - **R-A (Narrowed)**: Uncited sentences with a number + physical/measurement unit (extensible via Profile `content.extra_physical_units`) or a weak comparative paired with a numeric token trigger `major` (`UNCITED_FACTUAL_CLAIM`, blocking release); strong comparative wording alone without numbers triggers `minor` (warns without blocking release); planning/goal sentences without completed-fact cues, 4-digit years, and counting/planning units are exempt.
  - **R-B**: Every number in a cited sentence must appear with strict digit/dot boundaries in the cited evidence `quote_excerpt`.
  - **R-C**: Citing a nonexistent or L1-unmatched evidence ID rejects submission (`EVIDENCE_UNVERIFIED`).
  - **R-D**: Hypothesis sentences must not contain false experimental phrasing (`实验表明`, `results show`, etc.).
- **Release gates**:
  - Referenced sources without human-confirmed CSL-JSON bibliography block `render_document(purpose="release")` with `BIBLIOGRAPHY_MISSING` (`purpose="preview"` shows an explicit unconfirmed placeholder). Agents may suggest metadata via `propose_source_bibliography`, while confirmation requires `daf ui` or `daf sources set-bib`.
  - Undefined `@tbl:` / `@fig:` / `@eq:` targets block `render_document(purpose="release")` with `CROSSREF_UNRESOLVED`.
  - Only `builtin` and `mapped` template modes are supported (`tagged` raises `TEMPLATE_MODE_UNSUPPORTED`).

## 3. Independent Review & User Approvals

- **Mandatory identities & separation**: `start_review` and `submit_review` require `reviewer_id` / `reviewed_by` (`>= 3` non-whitespace chars; otherwise `REVIEWER_ID_REQUIRED`) distinct from `author_session` (`REVIEWER_IS_AUTHOR`).
- **Quote-anchored checklists**: Every non-`not_applicable` checklist item requires `rationale` `>= 15` characters and at least one `quotes` substring (`>= 10` normalized characters or matching a full Pandoc sentence); when more than 3 active checklist items exist, they must not all reuse the same single quote. `not_applicable` exempts quotes but requires a rationale of `>= 20` characters. Explicit `recommendation` (`approve` or `revise`) is mandatory.
- **No self-approval**: Agents must never run `daf approvals decide`, `daf sources set-bib`, or `daf ui` actions on the user's behalf. Ask the user to review and approve in `daf ui` or CLI.
