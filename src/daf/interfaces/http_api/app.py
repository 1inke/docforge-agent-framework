"""HTTP API Interface & Lightweight Status Inspector for DocForge Agent Framework (DAF §2.1.B, §7.1, §8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from daf.core.engine import DAFEngine
from daf.core.errors import (
    ApprovalConflictError,
    ArtifactPathInvalidError,
    ClaimReviewPendingError,
    ContextStaleError,
    ContractValidationError,
    DAFError,
    EvidenceChangedError,
    IdempotencyKeyConflictError,
    PandocUnavailableError,
    PDFExportError,
    PolicyConflictError,
    ProjectAlreadyExistsError,
    RenderStaleError,
    RevisionConflictError,
    SecurityBoundaryError,
    SemanticReviewRequiredError,
    SemanticReviewStaleError,
    SourceIndexStaleError,
    SourcePolicyChangeRequiresHumanError,
    SourceScopeDeniedError,
    SourceSliceNotFoundError,
    SourceSliceStaleError,
    TemplateRenderError,
    UntrustedApprovalChannelError,
)


class ToolInvokeRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


class HumanApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"] = "approved"
    decided_by: str = "human_user"
    comment: str = ""


def create_http_app(workspace_root: str | Path | None = None, allowed_source_roots: list[str | Path] | None = None) -> FastAPI:
    app = FastAPI(
        title="DocForge Agent Framework (DAF) HTTP API",
        version="1.4.0",
        description="Stateful, contract-enforced execution API for external AI Agents.",
    )

    def _engine() -> DAFEngine:
        return DAFEngine(workspace_root=workspace_root, allowed_source_roots=allowed_source_roots)

    @app.get("/api/v1/capabilities")
    def http_get_capabilities(profile_id: str | None = None) -> dict[str, Any]:
        try:
            return _engine().get_capabilities(profile_id=profile_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/profiles/{profile_id}")
    def http_get_profile(profile_id: str) -> dict[str, Any]:
        try:
            return _engine().get_document_profile(profile_id=profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/projects/{project_id}/status")
    def http_get_project_status(project_id: str) -> Any:
        try:
            return _engine().get_project_status(project_id=project_id)
        except (SecurityBoundaryError, ArtifactPathInvalidError) as exc:
            return JSONResponse(status_code=403, content=exc.to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/human-portal/projects/{project_id}/approvals/{request_id}/decide")
    def http_decide_approval_by_human(
        project_id: str,
        request_id: str,
        req: HumanApprovalDecisionRequest,
        x_daf_human_signature: str | None = Header(default=None),
    ) -> Any:
        eng = _engine()
        try:
            result = eng.decide_approval_by_human(
                project_id=project_id,
                approval_request_id=request_id,
                decision=req.decision,
                decided_by=req.decided_by,
                comment=req.comment,
                portal_signature=x_daf_human_signature,
                require_portal_signature=True,
            )
            return {"ok": True, "data": result}
        except UntrustedApprovalChannelError as exc:
            return JSONResponse(status_code=403, content=exc.to_dict())
        except (
            ApprovalConflictError,
            SemanticReviewRequiredError,
            SemanticReviewStaleError,
            ClaimReviewPendingError,
            EvidenceChangedError,
        ) as exc:
            return JSONResponse(status_code=409, content=exc.to_dict())
        except DAFError as exc:
            return JSONResponse(status_code=400, content=exc.to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/human-portal/projects/{project_id}/source-policy/{request_id}/decide")
    def http_decide_source_policy(
        project_id: str, request_id: str, req: HumanApprovalDecisionRequest,
        x_daf_human_signature: str | None = Header(default=None),
    ) -> Any:
        try:
            result = _engine().trusted_decide_source_policy_change(
                project_id, request_id, req.decision, req.decided_by, x_daf_human_signature,
            )
            return {"ok": True, "data": result}
        except SourcePolicyChangeRequiresHumanError as exc:
            return JSONResponse(status_code=403, content=exc.to_dict())
        except PolicyConflictError as exc:
            return JSONResponse(status_code=409, content=exc.to_dict())
        except DAFError as exc:
            return JSONResponse(status_code=400, content=exc.to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/tools/{tool_name}")
    def http_invoke_tool(tool_name: str, req: ToolInvokeRequest) -> Any:
        try:
            eng = _engine()
            result = eng.call_agent_tool(tool_name, req.params)
            return {"ok": True, "tool": tool_name, "data": result}
        except (
            SecurityBoundaryError,
            ArtifactPathInvalidError,
            UntrustedApprovalChannelError,
            SourceScopeDeniedError,
            SourcePolicyChangeRequiresHumanError,
        ) as exc:
            return JSONResponse(status_code=403, content=exc.to_dict())
        except (
            ProjectAlreadyExistsError,
            RevisionConflictError,
            ContextStaleError,
            IdempotencyKeyConflictError,
            ApprovalConflictError,
            RenderStaleError,
            SemanticReviewRequiredError,
            SemanticReviewStaleError,
            ClaimReviewPendingError,
            EvidenceChangedError,
            SourceSliceStaleError,
            SourceIndexStaleError,
            PolicyConflictError,
        ) as exc:
            return JSONResponse(status_code=409, content=exc.to_dict())
        except SourceSliceNotFoundError as exc:
            return JSONResponse(status_code=404, content=exc.to_dict())
        except ContractValidationError as exc:
            return JSONResponse(status_code=422, content=exc.to_dict())
        except (TemplateRenderError, PandocUnavailableError, PDFExportError) as exc:
            return JSONResponse(status_code=500, content=exc.to_dict())
        except DAFError as exc:
            return JSONResponse(status_code=400, content=exc.to_dict())
        except ValueError as exc:
            if "Unknown DAF tool" in str(exc):
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
