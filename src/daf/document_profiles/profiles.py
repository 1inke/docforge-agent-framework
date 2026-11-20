"""Extensible Document Profile Registry (Content + Structure + Layout Layers) for DAF.

This core framework ships without domain-specific templates or document presets.
Use the neutral ``default`` profile with ``profile_overrides``, or register custom
``DocumentProfileContract`` instances via :func:`register_document_profile`.
"""

from __future__ import annotations

from typing import Any

from daf.core.contracts import (
    ContentProfile,
    DocumentProfileContract,
    LayoutProfile,
    StructureProfile,
    TemplateInspectionReport,
)

BUILTIN_PROFILES: dict[str, DocumentProfileContract] = {
    "default": DocumentProfileContract(
        profile_id="default",
        display_name="Default Neutral Profile",
        description=(
            "Template-agnostic neutral document profile. Supply custom structure, "
            "word budgets, review checklists, and layout rules via profile_overrides "
            "or register_document_profile()."
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


def register_document_profile(profile: DocumentProfileContract) -> None:
    """Register a custom DocumentProfileContract at runtime."""
    _CUSTOM_PROFILES[profile.profile_id] = profile.model_copy(deep=True)


def unregister_document_profile(profile_id: str) -> None:
    """Remove a previously registered custom DocumentProfileContract."""
    _CUSTOM_PROFILES.pop(profile_id, None)


def clear_custom_profiles() -> None:
    """Clear all runtime-registered custom profiles."""
    _CUSTOM_PROFILES.clear()


def _all_profiles() -> dict[str, DocumentProfileContract]:
    return {**BUILTIN_PROFILES, **_CUSTOM_PROFILES}


def get_document_profile(profile_id: str) -> DocumentProfileContract:
    """Return a deep copy of the requested DocumentProfileContract."""
    profiles = _all_profiles()
    if profile_id not in profiles:
        supported = ", ".join(sorted(profiles.keys()))
        raise ValueError(f"Unknown profile_id '{profile_id}'. Supported profiles: {supported}")
    return profiles[profile_id].model_copy(deep=True)


def list_profile_summaries() -> list[dict[str, Any]]:
    """Return lightweight metadata for all registered profiles."""
    summaries: list[dict[str, Any]] = []
    for pid, prof in _all_profiles().items():
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
                        f"match required profile section IDs {sorted(missing_placeholders)}. "
                        "Provide explicit placeholder mappings or ensure placeholders match section IDs."
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
