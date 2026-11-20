# DAF Agent Protocol (v2.0)

## 1. Streamlined MCP Tool Surface

| Group | Tools |
| --- | --- |
| Discovery | `get_capabilities` (compact summary < 3000 chars; pass `include_schemas` on demand), `get_task_guidance` |
| Project | `create_project`, `get_project_status` |
| Sources | `ingest_sources`, `find_sources` (list or literal search), `read_source_block`, `register_evidence` |
| Writing | `submit_outline`, `get_section_context` (with optional `include_content_window`), `submit_section`, `record_agent_note` |
| Review | `start_review` (QA + create & claim tasks), `submit_review` (structured quotes, submit-is-accept) |
| Approval & Release | `request_approval` (supports `query_only=True`), `render_document`, `export_document` |

All MCP tools return structured `DAFError` JSON on failure and write tools include `next_action`.

## 2. Inline Citation & Grounding Rules (P0-1)

- Cite registered evidence inline using Pandoc syntax: `Latency dropped to 9.2 ms[@ev_01]` or `[@ev_01; @ev_02]`.
- Mark unverified research hypotheses inline: `We hypothesize linear scaling[@hypothesis]`.
- `submit_section` parses `content_markdown` with `pandoc -f markdown -t json` and enforces:
  - **R-A**: Uncited sentences containing numeric+unit measurements or comparative claims trigger `UNCITED_FACTUAL_CLAIM`.
  - **R-B**: Every number in a cited sentence must appear with strict digit/dot boundaries in the cited evidence `quote_excerpt`.
  - **R-C**: Citing a nonexistent or L1-unmatched evidence ID rejects submission (`EVIDENCE_UNVERIFIED`).
  - **R-D**: Hypothesis sentences must not contain false experimental phrasing (`实验表明`, `results show`, etc.).

## 3. Independent Review & User Approvals

- **Independent reviewer**: Semantic review must be executed by a separate subagent/session (`reviewer_id != author_session`; otherwise `REVIEWER_IS_AUTHOR`).
- **Quote-anchored checklists**: Every non-`not_applicable` checklist item must provide at least one `quotes` substring present in the section text; `not_applicable` requires a rationale of at least 20 characters.
- **No self-approval**: Agents must never run `daf approvals decide` or `daf ui` on the user's behalf. Ask the user to review and approve in `daf ui` or `daf approvals show/decide`.
