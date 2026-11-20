"""Localhost-only (127.0.0.1) human review & approval UI with one-time session token (P0-3)."""

from __future__ import annotations

import html
import hmac
import secrets
import socket
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Form, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from daf.core.engine import DAFEngine
from daf.core.errors import ApprovalConflictError, DAFError


def _esc(val: Any) -> str:
    return html.escape(str(val or ""))


def create_local_ui_app(
    workspace_root: str | Path | None = None,
    allowed_source_roots: list[str | Path] | None = None,
    access_token: str | None = None,
) -> tuple[FastAPI, str]:
    """Create the local 127.0.0.1 approval UI app and its required access token."""
    token = access_token or secrets.token_urlsafe(24)
    engine = DAFEngine(workspace_root=workspace_root, allowed_source_roots=allowed_source_roots)

    app = FastAPI(
        title="DAF Local Approval Portal",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
    )

    def _require_token(query_token: str | None, header_token: str | None) -> None:
        supplied = query_token or header_token or ""
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=403, detail="Missing or invalid local UI access token.")

    @app.get("/", response_class=HTMLResponse)
    def ui_home(
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> HTMLResponse:
        _require_token(token_q, x_daf_ui_token)
        projects = engine.store.list_projects()
        rows = "".join(
            f"<li><a href='/projects/{_esc(p['project_id'])}?token={_esc(token)}'>"
            f"{_esc(p['project_id'])}</a> — stage: {_esc(p['stage'])} (v{_esc(p['state_version'])})</li>"
            for p in projects
        )
        body = f"""<!doctype html><html><head><meta charset='utf-8'><title>DAF Local UI</title>
        <style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;}}</style>
        </head><body><h1>DAF Local Approval Portal</h1>
        <p>Workspace: <code>{_esc(engine.workspace_root)}</code></p>
        <h2>Projects</h2><ul>{rows or '<li>No projects yet.</li>'}</ul></body></html>"""
        return HTMLResponse(content=body)

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def ui_project(
        project_id: str,
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> HTMLResponse:
        _require_token(token_q, x_daf_ui_token)
        state = engine.store.load_state(project_id)
        approvals_html: list[str] = []
        for req in state.approval_requests.values():
            detail = engine.show_approval_detail(project_id, req.approval_request_id)
            diff_blocks = ""
            if "outline" in detail:
                nodes_li = "".join(
                    f"<li><strong>{_esc(n['title'])}</strong> ({_esc(n['section_id'])}) "
                    f"[{n['word_budget']['min_words']}-{n['word_budget']['max_words']} words]</li>"
                    for n in detail["outline"]["nodes"]
                )
                diff_blocks = f"<p>Thesis: {_esc(detail['outline']['global_thesis'])}</p><ul>{nodes_li}</ul>"
            elif "sections" in detail:
                diff_blocks = "".join(
                    f"<h4>{_esc(s['title'])} (r{s['revision']}, review={_esc(s['review_status'])})</h4>"
                    f"<pre>{_esc(s['diff'] or '(no diff)')}</pre>"
                    for s in detail["sections"]
                )
            action_form = ""
            if req.status == "pending":
                action_form = (
                    f"<form method='post' action='/projects/{_esc(project_id)}/approvals/{_esc(req.approval_request_id)}/decide?token={_esc(token)}'>"
                    f"<input type='hidden' name='expected_artifact_hash' value='{_esc(req.artifact_hash)}'/>"
                    f"<input type='text' name='comment' placeholder='Optional comment'/> "
                    f"<button name='decision' value='approved'>Approve</button> "
                    f"<button name='decision' value='rejected'>Reject</button></form>"
                )
            approvals_html.append(
                f"<div style='border:1px solid #ccc;padding:1rem;margin:1rem 0;'>"
                f"<h3>{_esc(req.artifact_type)} ({_esc(req.approval_request_id)}) — {_esc(req.status)}</h3>"
                f"<p>Hash: <code>{_esc(req.artifact_hash)}</code></p>{diff_blocks}{action_form}</div>"
            )

        ev_rows = "".join(
            f"<li><code>{_esc(ev.evidence_id)}</code> [{_esc(ev.source_reliability)}] {_esc(ev.quote_excerpt[:100])} "
            f"<form style='display:inline' method='post' action='/projects/{_esc(project_id)}/evidence/{_esc(ev.evidence_id)}/reliability?token={_esc(token)}'>"
            f"<button name='reliability' value='trusted'>Mark Trusted</button></form></li>"
            for ev in state.evidence_ledger.items.values()
        )
        notes_rows = "".join(
            f"<li><code>{_esc(e.decision_id)}</code>: {_esc(e.summary)} "
            f"<form style='display:inline' method='post' action='/projects/{_esc(project_id)}/notes/{_esc(e.decision_id)}/adopt?token={_esc(token)}'>"
            f"<button type='submit'>Adopt as Constraint</button></form></li>"
            for e in state.decision_log.entries
            if e.source == "agent_note" and not e.adopted_as_constraint
        )
        preview_link = (
            f"<p><a href='/projects/{_esc(project_id)}/preview-docx?token={_esc(token)}'>Download Preview DOCX</a></p>"
            if state.last_rendered_docx_path and Path(state.last_rendered_docx_path).is_file()
            else ""
        )
        body = f"""<!doctype html><html><head><meta charset='utf-8'><title>{_esc(project_id)}</title>
        <style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;}}pre{{background:#f6f6f6;padding:.5rem;overflow:auto;}}</style>
        </head><body><h1>{_esc(state.brief.title)} ({_esc(project_id)})</h1>
        <p>Stage: <strong>{_esc(state.stage)}</strong> | Profile: <code>{_esc(state.profile.profile_id)}</code></p>
        {preview_link}
        <h2>Approval Requests</h2>{''.join(approvals_html) or '<p>No approval requests.</p>'}
        <h2>Evidence Reliability (L3)</h2><ul>{ev_rows or '<li>No evidence registered.</li>'}</ul>
        <h2>Pending Agent Notes</h2><ul>{notes_rows or '<li>No pending agent notes.</li>'}</ul>
        <h2>Add User Constraint</h2>
        <form method='post' action='/projects/{_esc(project_id)}/constraints?token={_esc(token)}'>
          <input type='text' name='constraint' placeholder='User constraint' required style='width:60%'/>
          <button type='submit'>Add Constraint</button>
        </form></body></html>"""
        return HTMLResponse(content=body)

    @app.post("/projects/{project_id}/approvals/{request_id}/decide")
    def ui_decide_approval(
        project_id: str,
        request_id: str,
        decision: Literal["approved", "rejected"] = Form(...),
        expected_artifact_hash: str = Form(default=""),
        comment: str = Form(default=""),
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> Any:
        _require_token(token_q, x_daf_ui_token)
        state = engine.store.load_state(project_id)
        req = state.approval_requests.get(request_id)
        if req is None:
            raise HTTPException(status_code=404, detail=f"Unknown approval_request_id '{request_id}'.")
        if expected_artifact_hash and req.artifact_hash != expected_artifact_hash:
            err = ApprovalConflictError(
                "Approval request hash changed since page load.",
                code="APPROVAL_STALE",
                details={"expected": expected_artifact_hash, "actual": req.artifact_hash},
            )
            return JSONResponse(status_code=409, content=err.to_dict())
        try:
            res = engine.decide_approval_by_human(
                project_id=project_id,
                approval_request_id=request_id,
                decision=decision,
                decided_by="local_user",
                confirmation_channel="local_ui",
                comment=comment,
            )
            return JSONResponse(status_code=200, content={"ok": True, "data": res})
        except ApprovalConflictError as exc:
            return JSONResponse(status_code=409, content=exc.to_dict())
        except DAFError as exc:
            return JSONResponse(status_code=400, content=exc.to_dict())

    @app.post("/projects/{project_id}/evidence/{evidence_id}/reliability")
    def ui_set_evidence_reliability(
        project_id: str,
        evidence_id: str,
        reliability: Literal["trusted", "conditional", "unassessed", "rejected"] = Form(...),
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> Any:
        _require_token(token_q, x_daf_ui_token)
        res = engine.set_evidence_reliability_by_user(project_id, evidence_id, reliability)
        return JSONResponse(status_code=200, content={"ok": True, "data": res})

    @app.post("/projects/{project_id}/notes/{decision_id}/adopt")
    def ui_adopt_note(
        project_id: str,
        decision_id: str,
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> Any:
        _require_token(token_q, x_daf_ui_token)
        res = engine.adopt_agent_note_by_user(project_id, decision_id)
        return JSONResponse(status_code=200, content={"ok": True, "data": res})

    @app.post("/projects/{project_id}/constraints")
    def ui_add_constraint(
        project_id: str,
        constraint: str = Form(...),
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> Any:
        _require_token(token_q, x_daf_ui_token)
        res = engine.add_user_decision_by_user(
            project_id=project_id,
            feedback_text=constraint,
            add_user_constraints=[constraint],
        )
        return RedirectResponse(url=f"/projects/{project_id}?token={token}", status_code=303)

    @app.get("/projects/{project_id}/preview-docx")
    def ui_download_preview(
        project_id: str,
        token_q: str | None = Query(default=None, alias="token"),
        x_daf_ui_token: str | None = Header(default=None),
    ) -> Any:
        _require_token(token_q, x_daf_ui_token)
        state = engine.store.load_state(project_id)
        if not state.last_rendered_docx_path or not Path(state.last_rendered_docx_path).is_file():
            raise HTTPException(status_code=404, detail="No rendered DOCX available.")
        return FileResponse(path=state.last_rendered_docx_path, filename=Path(state.last_rendered_docx_path).name)

    return app, token


def serve_local_ui(
    workspace_root: str | Path | None = None,
    allowed_source_roots: list[str | Path] | None = None,
    port: int = 0,
) -> None:
    """Start the local approval UI bound strictly to 127.0.0.1 with a random free port and print tokenized URL."""
    import uvicorn

    host = "127.0.0.1"
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])

    app, token = create_local_ui_app(
        workspace_root=workspace_root,
        allowed_source_roots=allowed_source_roots,
    )
    print(f"DAF Local Approval UI listening on: http://{host}:{port}/?token={token}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
