"""
人物背景配置

支持两种使用方式：
  1. 默认内置 Persona（DEFAULT_PERSONA）
  2. 外部系统通过 API 传入自定义 Persona dict，实现多品牌/多角色隔离
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_EXTRA_MAX_LEN = 2000
_SYSTEM_OVERRIDE_PATTERNS = re.compile(
    r"ignore\s+all|forget\s+(?:previous|all)|you\s+are\s+now\s+a|"
    r"忘记所有|忽略所有|你现在是(?!.*客服)|新的?系统提示词?",
    re.IGNORECASE,
)


def _sanitize_extra(text: str) -> str:
    """限制 extra_instructions 长度，过滤覆盖系统角色的指令。"""
    if not text:
        return ""
    text = text[:_EXTRA_MAX_LEN]
    if _SYSTEM_OVERRIDE_PATTERNS.search(text):
        logger.warning("[persona] extra_instructions 含敏感指令，已清空")
        return ""
    return text


@dataclass
class PersonaConfig:
    """
    客服人物背景配置。

    可通过 PersonaConfig.from_dict() 从外部 API 请求动态构造，
    实现同一套智能体服务多个品牌/多种角色。
    """
    name: str = ""
    role: str = "专业客服"
    brand: str = ""
    personality_traits: list[str] = field(default_factory=lambda: [
        "专业自信", "亲切温柔", "活泼自然", "高情商",
    ])
    style_guidelines: list[str] = field(default_factory=lambda: [
        "使用礼貌称谓「您」",
        "回复简洁有力，聚焦产品价值，避免冗长",
        "不编造、不夸大产品功效，严格以知识库为准",
        "遇到无法解决的问题主动提出转人工",
        "不轻易承诺无法实现的事情",
    ])
    brand_intro: str = ""
    chitchat_topics: list[str] = field(default_factory=list)
    extra_instructions: str = ""
    accuracy_rules: dict = field(default_factory=lambda: {
        "priority": "所有产品信息以知识库内容为最高优先级，知识库无记录的内容一律不作答。",
        "fallback": "对于不确定的问题，统一回复：「关于这个问题我需要核实一下，建议您联系人工客服获取准确信息」",
    })
    business: Optional[dict] = None  # BusinessRuleBlock 的原始字典，None 时走兼容默认规则

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaConfig":
        """
        从字典构造 PersonaConfig（外部 API 传入时使用）。
        未提供的字段自动使用默认值，保持向后兼容。

        Args:
            data: 外部传入的 persona 字典

        Returns:
            PersonaConfig 实例
        """
        return cls(
            name=data.get("name", ""),
            role=data.get("role", "专业客服"),
            brand=data.get("brand", ""),
            personality_traits=data.get("personality_traits") or [
                "专业自信", "亲切温柔", "活泼自然", "高情商",
            ],
            style_guidelines=data.get("style_guidelines") or [
                "使用礼貌称谓「您」",
                "回复简洁有力，聚焦产品价值",
            ],
            brand_intro=data.get("brand_intro", ""),
            chitchat_topics=data.get("chitchat_topics") or [],
            extra_instructions=_sanitize_extra(data.get("extra_instructions", "")),
            accuracy_rules=data.get("accuracy_rules") or {},
            business=data.get("business") or data.get("business_rules"),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（存入 State 或日志用）。"""
        return {
            "name":                self.name,
            "role":                self.role,
            "brand":               self.brand,
            "personality_traits":  self.personality_traits,
            "style_guidelines":    self.style_guidelines,
            "brand_intro":         self.brand_intro,
            "chitchat_topics":     self.chitchat_topics,
            "extra_instructions":  self.extra_instructions,
            "accuracy_rules":      self.accuracy_rules,
            "business":            self.business,
        }

    def to_prompt_text(self) -> str:
        """渲染为系统 Prompt 中的人物背景段落。"""
        traits = "、".join(self.personality_traits or [])
        styles = "\n".join(f"- {s}" for s in (self.style_guidelines or []))
        topics = "、".join(self.chitchat_topics or [])
        if self.name:
            name_line = f"，你的名字叫「{self.name}」"
        else:
            name_line = ""
        role_text = (
            f"你属于「{self.brand}」品牌，你是{self.brand}的{self.role}{name_line}"
        ) if self.brand else f"你是{self.role}{name_line}"
        text = (
            f"【身份硬约束 — 必须遵守】\n"
            f"{role_text}。\n"
            f"你的品牌是「{self.brand}」，你只代表这个品牌。严禁冒用或混入其他任何品牌名称。"
        )
        if self.name:
            text += f"\n介绍自己时须带上名字，例如「我是{self.brand}的{self.role}{self.name}」。"
        text += (
            f"\n\n品牌背景：{self.brand_intro}\n\n"
            f"性格特征：{traits}。\n\n"
            f"话术规范：\n{styles}\n\n"
            f"闲聊时可以主动引导话题到：{topics}。"
        )
        if self.extra_instructions:
            text += f"\n\n【额外指令】\n{self.extra_instructions}"
        # 准确性规则注入
        acc = self.accuracy_rules or {}
        acc_parts = []
        if acc.get("deny"):
            acc_parts.append(f"【禁止回答范围】\n{acc['deny']}")
        if acc.get("priority"):
            acc_parts.append(f"【知识库优先级】\n{acc['priority']}")
        if acc.get("scenarios"):
            acc_parts.append(f"【高频场景处理规则】\n{acc['scenarios']}")
        if acc.get("fallback"):
            acc_parts.append(f"【不确定时标准话术】\n{acc['fallback']}")
        if acc_parts:
            text += "\n\n【回答准确性规则】\n" + "\n\n".join(acc_parts)
        return text


DEFAULT_PERSONA = PersonaConfig()
