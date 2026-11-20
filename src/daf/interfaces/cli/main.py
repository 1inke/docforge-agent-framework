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

    p_ui = subparsers.add_parser("ui", help="Launch localhost-only (127.0.0.1) human approval UI.")
    p_ui.add_argument("--port", type=int, default=0)

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

    # Local user approval commands (P0-3)
    p_appr = subparsers.add_parser("approvals", help="Local user approval management (list / show / decide).")
    appr_sub = p_appr.add_subparsers(dest="approvals_cmd", required=True)

    p_appr_list = appr_sub.add_parser("list", help="List approval requests for a project.")
    p_appr_list.add_argument("--project-id", required=True)

    p_appr_show = appr_sub.add_parser("show", help="Show outline/diff details for an approval request.")
    p_appr_show.add_argument("request_id", type=str)
    p_appr_show.add_argument("--project-id", required=True)

    p_appr_decide = appr_sub.add_parser("decide", help="Approve or reject an approval request.")
    p_appr_decide.add_argument("request_id", type=str)
    p_appr_decide.add_argument("--project-id", required=True)
    grp = p_appr_decide.add_mutually_exclusive_group(required=True)
    grp.add_argument("--approve", action="store_true")
    grp.add_argument("--reject", action="store_true")
    p_appr_decide.add_argument("--comment", type=str, default="")
    p_appr_decide.add_argument("--decided-by", type=str, default="local_user")

    # Local user profile apply (P0-2A)
    p_profile = subparsers.add_parser("profile", help="Local user profile commands.")
    prof_sub = p_profile.add_subparsers(dest="profile_cmd", required=True)
    p_prof_apply = prof_sub.add_parser("apply", help="Apply a workspace YAML profile to a project.")
    p_prof_apply.add_argument("--project-id", required=True)
    p_prof_apply.add_argument("--profile-id", required=True)

    # Local user evidence reliability grading (P0-1)
    p_ev = subparsers.add_parser("evidence", help="Local user evidence reliability commands.")
    ev_sub = p_ev.add_subparsers(dest="evidence_cmd", required=True)
    p_ev_trust = ev_sub.add_parser("trust", help="Set L3 reliability for an evidence item.")
    p_ev_trust.add_argument("--project-id", required=True)
    p_ev_trust.add_argument("--evidence-id", required=True)
    p_ev_trust.add_argument("--reliability", choices=["trusted", "conditional", "unassessed", "rejected"], default="trusted")

    # Local user constraints (P0-2B)
    p_con = subparsers.add_parser("constraints", help="Local user constraints & forbidden scopes.")
    con_sub = p_con.add_subparsers(dest="constraints_cmd", required=True)
    p_con_add = con_sub.add_parser("add", help="Add user constraint / forbidden scope / conflict ack.")
    p_con_add.add_argument("--project-id", required=True)
    p_con_add.add_argument("--text", required=True)
    p_con_add.add_argument("--constraint", action="append", default=[])
    p_con_add.add_argument("--forbidden-scope", action="append", default=[])
    p_con_add.add_argument("--ack-template-conflict", action="append", default=[])

    # Generic Agent tool dispatcher
    p_call = subparsers.add_parser("call", help="Invoke any non-privileged DAFEngine Agent tool with a JSON payload.")
    p_call.add_argument("tool_name", type=str)
    p_call.add_argument("--params", type=str, default="{}")

    args = parser.parse_args(argv)

    if args.command == "ui":
        from daf.interfaces.local_ui.app import serve_local_ui

        serve_local_ui(
            workspace_root=args.workspace,
            allowed_source_roots=args.allowed_source_root,
            port=args.port,
        )
        return {"status": "stopped"}

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
    if args.command == "approvals":
        if args.approvals_cmd == "list":
            return engine.list_approvals(args.project_id)
        if args.approvals_cmd == "show":
            return engine.show_approval_detail(args.project_id, args.request_id)
        if args.approvals_cmd == "decide":
            return engine.decide_approval_by_human(
                project_id=args.project_id,
                approval_request_id=args.request_id,
                decision="approved" if args.approve else "rejected",
                decided_by=args.decided_by,
                confirmation_channel="local_cli",
                comment=args.comment,
            )
    if args.command == "profile" and args.profile_cmd == "apply":
        return engine.apply_profile_by_user(args.project_id, args.profile_id)
    if args.command == "evidence" and args.evidence_cmd == "trust":
        return engine.set_evidence_reliability_by_user(args.project_id, args.evidence_id, args.reliability)
    if args.command == "constraints" and args.constraints_cmd == "add":
        return engine.add_user_decision_by_user(
            project_id=args.project_id,
            feedback_text=args.text,
            add_user_constraints=args.constraint,
            add_forbidden_scopes=args.forbidden_scope,
            acknowledge_template_conflicts=args.ack_template_conflict,
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
