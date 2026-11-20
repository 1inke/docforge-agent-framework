"""Extensible per-profile method guidance registry; DocumentProfile owns structural constraints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from daf.guidance.policy import canonical_hash


@dataclass(frozen=True)
class ProfileMethods:
    version: str
    tasks: dict[str, tuple[str, ...]]


class ProfileGuidanceRegistry:
    """Generic task-method registry with runtime registration for custom profiles."""

    profiles: ClassVar[dict[str, ProfileMethods]] = {
        "default": ProfileMethods(
            "1.0",
            {
                "source_analysis": (
                    "按主题、事实依据、边界条件与待确认缺口整理资料，区分原文陈述与推断。",
                ),
                "evidence_extraction": (
                    "登记精确可溯源的原文摘录、条件与口径，避免以主观总结替代原始依据。",
                ),
                "plan_outline": (
                    "围绕项目目标、章节依赖与核验要求组织大纲结构，确保各节职责清晰无重叠。",
                ),
                "draft_section": (
                    "基于当前上下文与已核验证据撰写正文，保持术语、符号及数据口径一致。",
                ),
                "revise_section": (
                    "按审校问题、用户约束与禁改范围定向修订，修改核心结论时同步核对上下游一致性。",
                ),
                "review_section": (
                    "逐项核对结构完整性、证据支撑度、术语一致性与逻辑闭环，显式给出结论与问题列表。",
                ),
                "global_review": (
                    "跨章节检查整体论证闭环、数据口径统一性与未解决的证据缺口。",
                ),
                "release_preflight": (
                    "核对全部审校、主张绑定、人工审批与渲染产物状态，确认满足发布门禁条件。",
                ),
            },
        ),
    }

    @classmethod
    def register(cls, profile_id: str, methods: ProfileMethods) -> None:
        cls.profiles[profile_id] = methods

    @classmethod
    def unregister(cls, profile_id: str) -> None:
        if profile_id != "default":
            cls.profiles.pop(profile_id, None)

    @classmethod
    def resolve(cls, profile_id: str, task_kind: str) -> dict[str, object]:
        methods = cls.profiles.get(profile_id, cls.profiles["default"])
        selected = methods.tasks.get(task_kind, cls.profiles["default"].tasks[task_kind])
        payload = {
            "profile_id": profile_id,
            "version": methods.version,
            "task_kind": task_kind,
            "methods": list(selected),
        }
        return {
            **payload,
            "content_hash": canonical_hash(payload),
            "source": "daf.guidance.profile_guidance.ProfileGuidanceRegistry",
        }
