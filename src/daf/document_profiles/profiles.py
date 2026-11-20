"""Extensible Document Profile Registry (YAML workspace profiles + neutral default).

Users author domain/project profiles as YAML files under ``<workspace>/profiles/<profile_id>.yaml``.
Agents can read profiles but cannot modify or override profile rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from daf.core.contracts import (
    ContentProfile,
    DocumentProfileContract,
    LayoutProfile,
    SectionRule,
    StructureProfile,
    TemplateInspectionReport,
)

BUILTIN_PROFILES: dict[str, DocumentProfileContract] = {
    "default": DocumentProfileContract(
        profile_id="default",
        display_name="Default Neutral Profile",
        description=(
            "Template-agnostic neutral document profile. Place custom YAML files in "
            "<workspace>/profiles/<profile_id>.yaml to define project structure and rules."
        ),
        content_profile=ContentProfile(
            discourse_style="neutral",
            evidence_density="medium",
            tone_constraints=[],
            forbidden_phrases=[],
            terminology_consistency_required=True,
        ),
        structure_profile=StructureProfile(
            required_sections=[],
            optional_sections=[],
        ),
        layout_profile=LayoutProfile(),
        approval_policy=["outline", "final_draft"],
        block_release_on_major_issues=True,
    ),
}

_CUSTOM_PROFILES: dict[str, DocumentProfileContract] = {}


def parse_profile_yaml(raw: dict[str, Any], fallback_id: str = "") -> DocumentProfileContract:
    """Parse either a full DocumentProfileContract dict or the concise user YAML format."""
    if "structure_profile" in raw or "content_profile" in raw:
        data = dict(raw)
        if fallback_id and not data.get("profile_id"):
            data["profile_id"] = fallback_id
        return DocumentProfileContract.model_validate(data)

    profile_id = str(raw.get("profile_id") or fallback_id).strip()
    if not profile_id:
        raise ValueError("Profile YAML must specify 'profile_id'.")
    display_name = str(raw.get("display_name") or profile_id)
    description = str(raw.get("description") or "")

    required_sections: list[SectionRule] = []
    optional_sections: list[SectionRule] = []
    for sec in raw.get("sections") or []:
        sec_id = str(sec.get("id") or sec.get("section_id") or "").strip()
        if not sec_id:
            raise ValueError(f"Profile '{profile_id}' has a section entry missing 'id'.")
        words = sec.get("words")
        min_words = int(words[0]) if isinstance(words, list) and len(words) >= 1 else int(sec.get("min_words", 50))
        max_words = int(words[1]) if isinstance(words, list) and len(words) >= 2 else int(sec.get("max_words", 5000))
        is_req = bool(sec.get("required", True))
        rule = SectionRule(
            section_id=sec_id,
            title=str(sec.get("title") or sec_id),
            required=is_req,
            min_words=min_words,
            max_words=max_words,
            depends_on=list(sec.get("depends_on") or []),
            required_elements=list(sec.get("required_elements") or []),
            review_checklist=list(sec.get("review_checklist") or []),
        )
        if is_req:
            required_sections.append(rule)
        else:
            optional_sections.append(rule)

    content_cfg = raw.get("content") or {}
    layout_cfg = raw.get("layout") or {}
    approval_policy = list(raw.get("approval_policy") or ["outline", "final_draft"])
    for mandatory in ("outline", "final_draft"):
        if mandatory not in approval_policy:
            approval_policy.append(mandatory)

    return DocumentProfileContract(
        profile_id=profile_id,
        display_name=display_name,
        description=description,
        content_profile=ContentProfile.model_validate(content_cfg),
        structure_profile=StructureProfile(
            required_sections=required_sections,
            optional_sections=optional_sections,
        ),
        layout_profile=LayoutProfile.model_validate(layout_cfg),
        approval_policy=approval_policy,  # type: ignore[arg-type]
        block_release_on_major_issues=True,
    )


def load_workspace_profiles(workspace_root: str | Path | None) -> dict[str, DocumentProfileContract]:
    """Load all ``*.yaml`` / ``*.yml`` profile files from ``<workspace_root>/profiles/``."""
    loaded: dict[str, DocumentProfileContract] = {}
    if not workspace_root:
        return loaded
    prof_dir = Path(workspace_root) / "profiles"
    if not prof_dir.is_dir():
        return loaded
    for path in sorted(prof_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"Profile YAML '{path.name}' must be a mapping.")
            prof = parse_profile_yaml(data, fallback_id=path.stem)
            loaded[prof.profile_id] = prof
    return loaded


def register_document_profile(profile: DocumentProfileContract) -> None:
    """Register a custom DocumentProfileContract at runtime (programmatic / tests)."""
    _CUSTOM_PROFILES[profile.profile_id] = profile.model_copy(deep=True)


def unregister_document_profile(profile_id: str) -> None:
    _CUSTOM_PROFILES.pop(profile_id, None)


def clear_custom_profiles() -> None:
    _CUSTOM_PROFILES.clear()


def _all_profiles(workspace_root: str | Path | None = None) -> dict[str, DocumentProfileContract]:
    merged = dict(BUILTIN_PROFILES)
    merged.update(_CUSTOM_PROFILES)
    merged.update(load_workspace_profiles(workspace_root))
    return merged


def get_document_profile(
    profile_id: str,
    workspace_root: str | Path | None = None,
) -> DocumentProfileContract:
    """Return a deep copy of the requested DocumentProfileContract."""
    profiles = _all_profiles(workspace_root)
    if profile_id not in profiles:
        supported = ", ".join(sorted(profiles.keys()))
        raise ValueError(f"Unknown profile_id '{profile_id}'. Supported profiles: {supported}")
    return profiles[profile_id].model_copy(deep=True)


def list_profile_summaries(workspace_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return lightweight metadata for all registered profiles."""
    summaries: list[dict[str, Any]] = []
    for pid, prof in _all_profiles(workspace_root).items():
        summaries.append(
            {
                "profile_id": pid,
                "display_name": prof.display_name,
                "description": prof.description,
                "discourse_style": prof.content_profile.discourse_style,
                "required_section_ids": [s.section_id for s in prof.structure_profile.required_sections],
                "optional_section_ids": [s.section_id for s in prof.structure_profile.optional_sections],
            }
        )
    return summaries


def check_profile_template_conflicts(
    profile: DocumentProfileContract,
    template_report: TemplateInspectionReport | None,
) -> list[dict[str, str]]:
    """Detect conflicts or coverage gaps between a DocumentProfileContract and an inspected .docx template."""
    if template_report is None:
        return []

    conflicts: list[dict[str, str]] = []
    required_ids = {s.section_id for s in profile.structure_profile.required_sections}
    required_titles = {s.title for s in profile.structure_profile.required_sections}

    if template_report.template_mode == "tagged":
        detected = set(template_report.detected_placeholders)
        missing_placeholders = [
            sec_id
            for sec_id in required_ids
            if sec_id not in detected and f"section_{sec_id}" not in detected and "sections" not in detected
        ]
        if missing_placeholders and detected:
            conflicts.append(
                {
                    "type": "tagged_template_placeholder_mismatch",
                    "severity": "warning",
                    "message": (
                        f"Tagged template defines placeholders {sorted(detected)}, which do not directly "
                        f"match required profile section IDs {sorted(missing_placeholders)}."
                    ),
                }
            )
    elif template_report.template_mode == "mapped":
        headings = set(template_report.heading_Anchors)
        cells = set(template_report.writable_table_cells)
        if not headings and not cells:
            conflicts.append(
                {
                    "type": "mapped_template_no_anchors",
                    "severity": "major",
                    "message": (
                        "Template contains neither Jinja2 tags nor recognizable heading/table anchors. "
                        "Explicit section_mapping is required to render into this template."
                    ),
                }
            )
        elif headings and required_titles and not (headings & required_titles):
            conflicts.append(
                {
                    "type": "mapped_template_heading_divergence",
                    "severity": "info",
                    "message": (
                        "Template headings differ from profile section titles. "
                        "Use explicit section_mapping during render_document or align outline titles with template anchors."
                    ),
                }
            )
    return conflicts
