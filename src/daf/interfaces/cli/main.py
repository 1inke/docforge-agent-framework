"""CLI Interface for DocForge Agent Framework (DAF)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from daf.core.engine import DAFEngine
from daf.core.errors import DAFError


def _load_json_arg(raw_value: str) -> Any:
    """Parse inline JSON string or @path/to/file.json."""
    if raw_value.startswith("@"):
        file_path = Path(raw_value[1:]).resolve()
        return json.loads(file_path.read_text(encoding="utf-8"))
    return json.loads(raw_value)


def run_cli_args(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        prog="daf",
        description="DocForge Agent Framework (DAF) CLI — Local Single-User Execution Interface.",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Workspace root directory (defaults to DAF_WORKSPACE_ROOT or ./.daf_workspace).",
    )
    parser.add_argument(
        "--allowed-source-root",
        action="append",
        default=None,
        help="Allowed local import root directory; repeat for multiple roots.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_cap = subparsers.add_parser("get-capabilities", help="Discover DAF capabilities and schemas.")
    p_cap.add_argument("--profile-id", type=str, default=None)

    p_prof = subparsers.add_parser("get-document-profile", help="Get 3-layer DocumentProfileContract.")
    p_prof.add_argument("--profile-id", type=str, required=True)

    p_create = subparsers.add_parser("create-project", help="Create an isolated project workspace.")
    p_create.add_argument("--project-id", type=str, required=True)
    p_create.add_argument("--title", type=str, required=True)
    p_create.add_argument("--profile-id", type=str, default="default")
    p_create.add_argument("--domain", type=str, default="general")
    p_create.add_argument("--research-direction", type=str, default="")

    p_status = subparsers.add_parser("get-project-status", help="Inspect cross-session project status.")
    p_status.add_argument("--project-id", type=str, required=True)

    p_guidance = subparsers.add_parser("get-task-guidance", help="Read current deterministic task guidance.")
    p_guidance.add_argument("--project-id", required=True)
    p_guidance.add_argument("--task-kind", required=True)
    p_guidance.add_argument("--section-id", default=None)
    p_guidance.add_argument("--max-guidance-chars", type=int, default=8000)

    p_confirm = subparsers.add_parser("confirm-approval", help="Confirm a pending local ApprovalRequest.")
    p_confirm.add_argument("--project-id", type=str, required=True)
    p_confirm.add_argument("--request-id", type=str, required=True)
    p_confirm.add_argument("--decision", type=str, choices=["approved", "rejected"], default="approved")
    p_confirm.add_argument("--approved-by", type=str, default="local_user")
    p_confirm.add_argument("--comment", type=str, default="")

    p_call = subparsers.add_parser("call", help="Invoke any non-privileged DAFEngine Agent tool with a JSON payload.")
    p_call.add_argument("tool_name", type=str)
    p_call.add_argument("--params", type=str, default="{}")

    args = parser.parse_args(argv)
    engine = DAFEngine(workspace_root=args.workspace, allowed_source_roots=args.allowed_source_root)

    if args.command == "get-task-guidance":
        return engine.get_task_guidance(args.project_id, args.task_kind, args.section_id, args.max_guidance_chars)
    if args.command == "get-capabilities":
        return engine.get_capabilities(profile_id=args.profile_id)
    if args.command == "get-document-profile":
        return engine.get_document_profile(profile_id=args.profile_id)
    if args.command == "create-project":
        return engine.create_project(
            project_id=args.project_id,
            title=args.title,
            profile_id=args.profile_id,
            domain=args.domain,
            research_direction=args.research_direction,
        )
    if args.command == "get-project-status":
        return engine.get_project_status(project_id=args.project_id)
    if args.command == "confirm-approval":
        return engine.decide_approval_by_human(
            project_id=args.project_id,
            approval_request_id=args.request_id,
            decision=args.decision,  # type: ignore[arg-type]
            decided_by=args.approved_by,
            confirmation_channel="local_cli",
            comment=args.comment,
        )
    if args.command == "call":
        kwargs = _load_json_arg(args.params)
        if not isinstance(kwargs, dict):
            raise ValueError("--params must decode to a JSON object of keyword arguments.")
        return engine.call_agent_tool(args.tool_name, kwargs)

    raise ValueError(f"Unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        result = run_cli_args(argv)
        sys.stdout.write(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2) + "\n")
        return 0
    except DAFError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2) + "\n")
        return 1
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "retryable": False,
                        "recommended_tool": None,
                        "details": {},
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
