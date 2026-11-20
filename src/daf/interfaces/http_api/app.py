"""HTTP API Interface & Lightweight Status Inspector for DocForge Agent Framework."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
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


def create_http_app(
    workspace_root: str | Path | None = None,
    allowed_source_roots: list[str | Path] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="DocForge Agent Framework (DAF) HTTP API",
        version="2.0.0",
        description="Stateful, contract-enforced execution API for external AI Agents.",
    )
    engine = DAFEngine(workspace_root=workspace_root, allowed_source_roots=allowed_source_roots)

    @app.get("/api/v1/capabilities")
    def http_get_capabilities(profile_id: str | None = None) -> dict[str, Any]:
        try:
            return engine.get_capabilities(profile_id=profile_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/profiles/{profile_id}")
    def http_get_profile(profile_id: str) -> dict[str, Any]:
        try:
            return engine.get_document_profile(profile_id=profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/projects/{project_id}/status")
    def http_get_project_status(project_id: str) -> Any:
        try:
            return engine.get_project_status(project_id=project_id)
        except (SecurityBoundaryError, ArtifactPathInvalidError) as exc:
            return JSONResponse(status_code=403, content=exc.to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/tools/{tool_name}")
    def http_invoke_tool(tool_name: str, req: ToolInvokeRequest) -> Any:
        try:
            result = engine.call_agent_tool(tool_name, req.params)
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
