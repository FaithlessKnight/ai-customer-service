"""
情绪+意图合并分析节点（异步版）

将原来的 emotion_node 和 intent_node 合并为单次 LLM 调用，
同时并行预取 RAG 检索结果，节省整体延迟。

使用 with_structured_output 强制结构化输出。
"""
from __future__ import annotations
from functools import lru_cache

import asyncio
import json
import logging
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.customer_service.state import CustomerServiceState
from agents.customer_service.prompts import (
    ANALYSIS_SYSTEM, ANALYSIS_USER,
    HUMAN_TRANSFER_EMOTIONS, HUMAN_TRANSFER_SCORE_THRESHOLD,
)

logger = logging.getLogger(__name__)

# 预取 RAG 的意图（命中率高，值得提前检索）
_PREFETCH_INTENTS = {"knowledge", "unknown"}
_DEFAULT_COLLECTIONS = ["products", "sales_script"]

_SUMMARY_SCENE_HINTS: dict[str, tuple[str, ...]] = {
    "trial_not_used": (
        "体验装未使用", "还没用", "没用过", "未开封", "没开封", "没拆", "忘了用",
    ),
    "trial_used": (
        "体验装已使用", "试用过", "用了体验装", "使用感受", "反馈", "用后",
    ),
    "first_order_no_rebuy": (
        "首单后", "未复购", "两个月", "2个月", "快用完", "回购", "老客户",
    ),
}


# ── 客户场景推断（纯规则，无 LLM 开销）──────────────────────────
def _infer_scene(profile: dict[str, Any] | None, user_input: str = "") -> tuple[str | None, str]:
    """
    根据 customer_profile 推断客户生命周期场景。

    优先级高到低：
      1. do_not_disturb=True → None（不再触达）
      2. trial_received and not trial_used → trial_not_used
      3. trial_used and order_count==0 → trial_used
      4. order_count>=1 and days_since_last_order<90 → first_order_no_rebuy
      5. 其他 → None (default)

    Returns:
        (scene, source)：scene 为 None 时 source 为 "default"，否则为 "inferred"。
    """
    if not profile:
        return None, "default"

    # CRM 若已在画像中给出 scene，直接采用（最高优先）
    explicit_scene = profile.get("scene")
    if isinstance(explicit_scene, str) and explicit_scene.strip():
        return explicit_scene.strip(), "crm_profile"

    if profile.get("do_not_disturb"):
        return None, "inferred"

    trial_received = bool(profile.get("trial_received"))
    trial_used     = bool(profile.get("trial_used"))
    order_count    = int(profile.get("order_count") or 0)
    days_since     = profile.get("days_since_last_order")

    if trial_received and not trial_used:
        return "trial_not_used", "inferred"
    if trial_used and order_count == 0:
        return "trial_used", "inferred"
    if order_count >= 1 and isinstance(days_since, (int, float)) and days_since < 90:
        return "first_order_no_rebuy", "inferred"
    # 摘要兜底：用 CRM 历史摘要 + 当前用户输入做关键词匹配
    summary = " ".join(
        str(profile.get(k, "") or "")
        for k in ("summary", "history_summary", "customer_profile_summary")
    ).strip()
    text = f"{summary} {user_input}".strip()
    if text:
        for scene_name, hints in _SUMMARY_SCENE_HINTS.items():
            if any(h in text for h in hints):
                return scene_name, "inferred_summary"
    return None, "default"


def _resolve_scene(state: CustomerServiceState) -> tuple[str | None, str]:
    """
    确定本轮使用的 scene：
      CRM 显式传入 > customer_profile 推断 > None。
    """
    scene = state.get("scene")
    if scene:
        return scene, state.get("scene_source") or "crm"
    return _infer_scene(state.get("customer_profile"), state.get("user_input", ""))


def _resolve_journey_stage(state: CustomerServiceState, scene: str | None) -> str | None:
    """
    解析 journey_stage（极简版）：
    - 仅保留未回复阶段：awaiting_reply_1 ... awaiting_reply_5
    - CRM 直接传枚举 1/2/3/4/5（int 或 str）会被归一化为 awaiting_reply_N
    - human_handled=True 作为独立控制信号使用，不再映射 journey_stage
    - 优先级：显式传入 > customer_profile 映射 > None（不再根据 consecutive_no_reply 推断）
    """
    _ = scene  # 预留参数，当前极简模式不使用 scene 细分阶段

    valid = {f"awaiting_reply_{i}" for i in range(1, 6)}

    def _norm(v: Any) -> str | None:
        # CRM 枚举：纯整数 1~5（int 或可解析为 int 的 str）
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            n = v
        elif isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            if s in valid:
                return s
            if s == "awaiting_reply":
                return "awaiting_reply_1"
            if s.isdigit():
                n = int(s)
            else:
                return None
        else:
            return None
        if 1 <= n <= 5:
            return f"awaiting_reply_{n}"
        return None

    js = _norm(state.get("journey_stage"))
    if js:
        return js

    profile = state.get("customer_profile") or {}
    for key in ("journey_stage", "stage", "journeyStage"):
        js = _norm(profile.get(key))
        if js:
            return js

    return None

# 退货 / 退款 / 投诉类：命中即静默转人工（不调生成器，由 CRM 发话 + 可配 webhook）
_AFTER_SALES_KEYWORDS: frozenset[str] = frozenset(
    {
        # 退款退货
        "退款", "退货", "退钱", "退回", "不想要了", "申请退", "退换", "退单", "退掉", "退一下",
        # 投诉与售后风险
        "投诉", "举报", "差评", "假货", "维权", "理赔", "赔偿", "质量问题", "起诉", "仲裁",
        "12315", "工商局", "消协", "市场监管",
        "破损", "漏发", "少发", "发错", "发错货", "不对版",
    }
)

# 咨询语气信号：命中售后关键词但同时带这类问法时，视为客户在「问政策」，不应直接转人工
# 例："退货是否有运费险？" / "你们退款政策是怎样的" / "支持7天无理由吗" / "退货流程怎么走"
_AFTER_SALES_INQUIRY_HINTS: tuple[str, ...] = (
    "是否", "有没有", "有无", "支不支持", "支持吗", "可以吗", "能不能",
    "怎么算", "怎么走", "怎么办", "怎么处理", "如何", "怎样",
    "政策", "规则", "流程", "条件", "标准", "几天内", "多久", "运费险",
    "无理由", "有效期",
)

# 社交平台常见购买确认口令（如“扣1/回1/回复1/打1”）
_QUICK_BUY_SIGNAL_RE = re.compile(
    r"^\s*(?:扣\s*1|扣一|回\s*1|回复\s*1|打\s*1)\s*$",
    re.IGNORECASE,
)

# 明确“介绍产品/商品信息”问法：用于多意图兜底，避免 order 吞掉 knowledge
_KNOWLEDGE_ASK_RE = re.compile(
    r"(?:介绍(?:一下)?(?:你们|咱们|这个|这款)?(?:产品|商品)?|"
    r"(?:产品|商品).{0,6}(?:介绍|信息|详情|成分|功效|特点|卖点|怎么用|使用方法)|"
    r"这款.{0,8}(?:怎么样|好不好|适合|成分|功效)|"
    r"你们(?:产品|商品)(?:有哪些|是什么|介绍下))",
    re.IGNORECASE,
)

# 明确订单/物流问法：用于多意图兜底
_ORDER_ASK_RE = re.compile(
    r"(?:订单|单号|运单|物流|快递|发货|配送|签收|售后|退款|退货|到哪|多久到|什么时候到|查(?:询)?(?:一下)?(?:订单|物流)?)",
    re.IGNORECASE,
)


def _augment_mixed_intents(user_input: str, intents: list[str]) -> list[str]:
    """
    基于关键词的多意图兜底：
    - 同句同时出现「订单/物流」与「产品介绍/商品信息」时，强制包含 order + knowledge
    - 保持模型给出的主意图顺序，仅补齐缺失项
    """
    base = list(intents) if intents else ["unknown"]
    text = user_input or ""
    has_order_query = bool(_ORDER_ASK_RE.search(text))
    has_knowledge_query = bool(_KNOWLEDGE_ASK_RE.search(text))

    if has_order_query and has_knowledge_query:
        if "order" not in base:
            base.append("order")
        if "knowledge" not in base:
            base.append("knowledge")

    if len(base) > 1 and "unknown" in base:
        base = [x for x in base if x != "unknown"]

    dedup: list[str] = []
    for x in base:
        if x not in dedup:
            dedup.append(x)
    return dedup or ["unknown"]

# 投诉/破损这类**已发生**的售后事实陈述：即便有疑问句也属于售后，不在咨询豁免范围
_AFTER_SALES_HARD_KEYWORDS: frozenset[str] = frozenset(
    {"投诉", "举报", "差评", "假货", "维权", "理赔", "赔偿", "起诉", "仲裁",
     "12315", "工商局", "消协", "市场监管",
     "破损", "漏发", "少发", "发错", "发错货", "不对版"}
)

# 软性“换人/转人工”表达：先澄清一次，不直接触发转人工
_SOFT_HANDOFF_KEYWORDS: frozenset[str] = frozenset(
    {"换人", "换个客服", "转人工", "人工客服", "真人客服", "找人工", "找个真人", "换客服"}
)


def _after_sales_hit(text: str) -> bool:
    if not text:
        return False
    if not any(kw in text for kw in _AFTER_SALES_KEYWORDS):
        return False
    # 投诉/破损/赔偿等"已发生"的售后陈述：即便带疑问语气也算售后
    if any(kw in text for kw in _AFTER_SALES_HARD_KEYWORDS):
        return True
    # 命中的是 退货/退款/退换 这类「动作词」：再看是不是咨询语气
    # 仅当文本包含咨询信号时豁免（"退货是否有运费险" / "退款政策是怎样" / "支持7天无理由吗"）
    if any(hint in text for hint in _AFTER_SALES_INQUIRY_HINTS):
        return False
    return True


def _is_soft_handoff_probe(text: str) -> bool:
    s = (text or "").strip()
    if not s or len(s) > 20:
        return False
    if _after_sales_hit(s):
        return False
    return any(kw in s for kw in _SOFT_HANDOFF_KEYWORDS)


_LOGISTICS_ETA_INQUIRY_RE = re.compile(
    r"(?:什么时候|多久|几日|几天|到哪|到了吗|发出了吗|发货了吗|物流|快递|运单|配送|包裹|到货|揽收|派送|签收|进度|"
    r"快点|加急|能不能快|催一下|抓紧|尽快|催.{0,3}单|催件|催货|催派)",
    re.IGNORECASE,
)


def _is_routine_logistics_eta_query(user_input: str) -> bool:
    """
    带运单/单号语境的到货时间、物流进度追问，易被模型误判为「催促」而判 excited(negative)。
    属正常售前/售中咨询，校正为 calm 且不触发情绪转人工。
    """
    if not user_input:
        return False
    text = user_input.strip()
    if len(text) > 96:
        return False
    # 与售后/维权强信号重叠时不豁免
    if any(
        k in text
        for k in (
            "投诉",
            "举报",
            "差评",
            "退款",
            "退货",
            "骗子",
            "假的",
            "假货",
            "12315",
            "工商局",
            "起诉",
            "维权",
            "赔偿",
        )
    ):
        return False
    strong = any(
        w in text
        for w in (
            "气死",
            "黑心",
            "抢钱",
            "骗钱",
            "滚蛋",
            "垃圾",
            "傻",
            "无语",
            "什么破",
            "什么鬼",
            "他妈",
            "傻逼",
        )
    )
    if strong:
        return False
    if "!!!" in text or "！！！" in text or "???" in text or "？？？" in text:
        return False
    if not _LOGISTICS_ETA_INQUIRY_RE.search(text):
        return False
    has_long_no = bool(re.search(r"\d{8,}", text))
    if has_long_no:
        return True
    # 纯催促用语（快点/加急等）本身即物流时效诉求，不要求句子里出现「快递」二字
    _urgency_only = re.compile(
        r"(?:快点|加急|能不能快|催一下|抓紧|尽快|催.{0,3}单|催件|催货|催派)"
    )
    if _urgency_only.search(text):
        return True
    logistics_kw = (
        "快递",
        "物流",
        "运单",
        "包裹",
        "单号",
        "发货",
        "配送",
        "订单",
        "货到",
    )
    return any(k in text for k in logistics_kw)


def _is_price_objection(user_input: str) -> bool:
    """
    价格异议硬兜底：客户讲"贵/不便宜/太贵/还是贵/能便宜点"等议价表达，
    且没有明显愤怒情绪修饰时，识别为价格异议（销售场景常态，不应转人工）。
    LLM 偶发会把这类表达判成 excited(negative)，此函数用于事后校正。
    """
    if not user_input:
        return False
    text = user_input.strip()
    # 极端情绪修饰词出现则不归类为单纯价格异议
    strong_anger = any(w in text for w in (
        "气死", "黑心", "抢钱", "骗钱", "滚蛋", "投诉", "举报", "差评",
        "垃圾", "傻", "无语", "什么破", "什么鬼",
    ))
    if strong_anger:
        return False
    # 三连及以上感叹号/问号也排除（属于明显激烈情绪）
    if "！！！" in text or "!!!" in text or "？？？" in text or "???" in text:
        return False
    price_keywords = (
        "贵", "不便宜", "便宜点", "便宜些", "划算点",
        "降价", "打折", "优惠点", "太贵", "好贵",
    )
    return any(k in text for k in price_keywords)


# 整条仅为微信/企微「[表情名]」式片段（可连续多个），无其它文字——易被误判为愤怒，需与议价兜底类似做硬校正
_WECHAT_BRACKET_EMOJI_ONLY_RE = re.compile(r"^(?:\s*\[[^\[\]]{1,16}\]\s*)+$")

# 方括号内名称命中下列任一则视为攻击性/负面情绪表情，禁止按「无害表情」强制 calm
_HOSTILE_WECHAT_BRACKET_SUBSTR: frozenset[str] = frozenset(
    {
        "咒骂",
        "发怒",
        "生气",
        "鄙视",
        "骷髅",
        "菜刀",
        "炸弹",
        "便便",
        "敲打",
        "心碎",
        "骂人",
        "厌恶",
        "惊恐",
        "大哭",
        "流泪",
        "难过",
        "苦涩",
        "裂开",
        "囧",
        "吐",
        "呕",
    }
)


def _wechat_bracket_labels(text: str) -> list[str]:
    return re.findall(r"\[([^\[\]]{1,16})\]", (text or "").strip())


def _is_hostile_wechat_bracket_only(text: str) -> bool:
    """纯方括号表情串，且至少一个标签含攻击性/强负面情绪关键词。"""
    if not _is_wechat_bracket_emoji_only_message(text):
        return False
    for lab in _wechat_bracket_labels(text):
        if any(tok in lab for tok in _HOSTILE_WECHAT_BRACKET_SUBSTR):
            return True
    return False


def _is_wechat_bracket_emoji_only_message(text: str) -> bool:
    if not text or not text.strip():
        return False
    return bool(_WECHAT_BRACKET_EMOJI_ONLY_RE.fullmatch(text.strip()))


def _is_agent_persona_chitchat_message(text: str) -> bool:
    """
    询问客服性别、是否真人/机器人等人设向闲聊，易被误判为 angry。
    短句命中即视为 benign，不触发情绪转人工。
    """
    t = (text or "").strip()
    if not t or len(t) > 48:
        return False
    if any(k in t for k in ("订单", "物流", "退款", "运单", "发货", "单号", "货在哪", "没收到")):
        return False
    if "男的女的" in t or "男还是女" in t or "女还是男" in t:
        return True
    if "男生还是女生" in t or "女生还是男生" in t or "是男是女" in t:
        return True
    if len(t) <= 32 and re.search(r"你是(男|女)的?", t) and ("男" in t and "女" in t):
        return True
    if len(t) <= 32 and re.search(r"您是(男|女)的?", t) and ("男" in t and "女" in t):
        return True
    if len(t) <= 36 and (
        re.search(r"(是不是|吗|么|嘛).{0,8}(真人|机器人|人工智能)", t)
        or re.search(r"(真人|机器人|人工智能).{0,6}(吗|么|嘛|\?|？)", t)
        or re.search(r"你是(机器人|AI|人工智能)|您是(机器人|AI|人工智能)", t)
    ):
        return True
    return False


def _analysis_cache_should_store_corrected(user_input: str) -> bool:
    return _is_wechat_bracket_emoji_only_message(user_input) or _is_agent_persona_chitchat_message(
        user_input
    )


def _apply_benign_emotion_overrides(
    user_input: str,
    emotion: str,
    emotion_polarity: str,
    emotion_score: float,
    emotion_reason: str,
    intents_list: list[str],
    intent_score: float,
    intent_reason: str,
) -> tuple[str, str, float, str, list[str], float, str]:
    """
    对易被误判为强负面情绪、但实际无害的输入做硬校正（不触发 emotion 转人工）。
    """
    intents = list(intents_list) if intents_list else ["unknown"]
    isc, ir = intent_score, intent_reason

    if _is_wechat_bracket_emoji_only_message(user_input):
        if _is_hostile_wechat_bracket_only(user_input):
            if intents == ["unknown"]:
                intents = ["chitchat"]
                isc = max(isc, 0.85)
                ir = "微信负面表情"
            return (
                "angry",
                "negative",
                max(float(emotion_score), 0.93),
                "负面表情",
                intents,
                isc,
                ir,
            )
        if intents == ["unknown"]:
            intents = ["chitchat"]
            isc = max(isc, 0.88)
            ir = "微信表情"
        return ("calm", "neutral", 0.9, "微信表情", intents, isc, ir)

    if _is_agent_persona_chitchat_message(user_input):
        if emotion in ("angry", "excited") or intents == ["unknown"]:
            intents = ["chitchat"]
        isc = max(isc, 0.88)
        ir = "人设闲聊"
        return ("calm", "neutral", 0.92, "人设闲聊", intents, isc, ir)

    if _is_routine_logistics_eta_query(user_input) and emotion in ("angry", "excited"):
        return (
            "calm",
            "neutral",
            min(float(emotion_score), 0.52),
            "物流时效咨询",
            intents,
            isc,
            ir,
        )

    return (
        emotion,
        emotion_polarity,
        emotion_score,
        emotion_reason,
        intents,
        isc,
        ir,
    )


def _emotion_transfer_needed(
    emotion: str,
    emotion_polarity: str,
    emotion_score: float,
    human_handled: bool,
    user_input: str = "",
) -> bool:
    if human_handled:
        return False
    # 催促物流时效（含「快点」「加急」）易被误判 angry，仍应先解释政策而非情绪转人工
    if _is_routine_logistics_eta_query(user_input):
        return False
    # 价格异议（"贵/还是贵/便宜点"）销售常态：即便 LLM 判成 angry 或 excited(negative)，
    # 只要没有强愤怒/攻击性修饰词，都先走话术解释（对标产品对比、性价比说明），不转人工
    if _is_price_objection(user_input):
        return False
    if emotion == "angry":
        return True
    if (
        emotion == "excited"
        and emotion_polarity == "negative"
        and emotion_score >= HUMAN_TRANSFER_SCORE_THRESHOLD
    ):
        if _is_routine_logistics_eta_query(user_input):
            return False
        return True
    return False


def _resolve_transfer_reason(
    user_input: str,
    human_handled: bool,
    emotion: str,
    emotion_polarity: str,
    emotion_score: float,
) -> tuple[bool, str | None]:
    # “换人/转人工”这类模糊表达，先让机器人澄清一次，不立即转人工
    if _is_soft_handoff_probe(user_input):
        return False, None
    if _after_sales_hit(user_input):
        return True, "after_sales"
    if _emotion_transfer_needed(emotion, emotion_polarity, emotion_score, human_handled, user_input):
        return True, "emotion"
    return False, None

# 体验装已使用信号：客户描述已试用 → field_updates["trial_used"] = True
_TRIAL_USED_SIGNALS = {
    "用了", "试了", "用过", "试过", "洗了", "洗过", "试用了", "已经试", "体验过",
    "用完了", "用了一", "用了两", "用了几", "用了多", "洗了几次", "用了好几",
    "试了一", "试用一", "洗了一次", "用起来", "洗起来",
}
_TRIAL_NOT_USED_NEGATIVE = {
    "没用", "没用过", "还没用", "未使用", "还未使用", "没试", "还没试", "未试用",
}
_TRIAL_FEEDBACK_HINT_RE = re.compile(
    r"(?:还行|一般|不错|挺好|很好|满意|有点干|偏干|有点紧绷|不拔干|起泡|泡沫|清洁力|洗完|肤感)",
    re.IGNORECASE,
)

def _detect_crm_field_updates(user_input: str, scene: str | None) -> dict:
    """
    根据用户输入和当前场景，检测需要 CRM 更新的字段。
    返回需要更新的字段 dict（可为空）。
    """
    updates: dict = {}
    if not scene:
        return updates

    if scene == "trial_not_used":
        if any(sig in user_input for sig in _TRIAL_USED_SIGNALS):
            updates["trial_used"] = True
            logger.info("[analysis] 检测到体验装已使用信号，建议 CRM 更新 trial_used=True")
        # 兜底：客户给出明确“使用后反馈”但未说“用了/试了”时，同样视为已使用
        elif (
            _TRIAL_FEEDBACK_HINT_RE.search(user_input or "")
            and not any(neg in (user_input or "") for neg in _TRIAL_NOT_USED_NEGATIVE)
        ):
            updates["trial_used"] = True
            logger.info("[analysis] 检测到体验后反馈表达，建议 CRM 更新 trial_used=True")

    return updates

# ── 提示词注入防护 ────────────────────────────────────────
_INJECTION_PATTERNS = re.compile(
    r"(?:ignore|forget|disregard)\s+(?:previous|above|all)\s+(?:instructions?|rules?|prompts?)|"
    r"you\s+are\s+now|new\s+system\s+prompt|---+\s*system|<\s*system\s*>|"
    r"忘记.{0,10}指令|忽略.{0,10}(?:之前|上面)|你现在是|新的?系统提示",
    re.IGNORECASE,
)

# 伪造知识库 / 系统消息格式（用户输入中不应出现这些结构）
_FAKE_REFERENCE_PATTERNS = re.compile(
    r"\[参考\d*\]|"                            # [参考1] 伪造知识库引用
    r"\[(?:来源|引用|文档|知识库)\d*\]|"          # [来源1] [知识库2] 等变体
    r"\{\"role\"\s*:\s*\"system\"|"              # {"role": "system"} JSON 伪造
    r"<\s*/?system\s*>|"                        # <system> XML 伪造
    r"【(?:系统|参考|知识库|规则|指令).*?】",      # 【系统指令】【参考内容】等伪造
    re.IGNORECASE,
)

# 伪造规则/指令块（常与伪造引用一起出现）
_FAKE_DIRECTIVE_PATTERNS = re.compile(
    r"(?:新规则|规则补充|系统指令|附加指令|新指令)[:：].{0,160}?(?=(?:请问|这个|这款|那这个|那这款|另外|$))|"
    r"(?:从现在起|现在开始|接下来).{0,40}?(?:你必须|只能|务必).{0,160}?(?=(?:请问|这个|这款|那这个|那这款|另外|$))",
    re.IGNORECASE,
)

# 越权查看他人数据：应直接拒绝，不进入普通查单流程
_OTHER_USER_DATA_PATTERNS = re.compile(
    r"(?:用户\s*id\s*为|user\s*id\s*(?:为|=)?|查看|看看|查询).{0,40}?(?:admin|其他用户|别人|他人).{0,80}?(?:订单|聊天记录|对话记录)|"
    r"(?:admin|其他用户|别人|他人).{0,80}?(?:订单|聊天记录|对话记录)",
    re.IGNORECASE,
)

# 英文角色重置 + 无关问题（如数学题）
_UNRELATED_MATH_PATTERNS = re.compile(
    r"(?:what\s+is\s+\d+\s*[+\-*/]\s*\d+)|(?:\b\d+\s*[+\-*/]\s*\d+\b)|(?:\b2\s*\+\s*2\b)|(?:\b1\s*\+\s*1\b)",
    re.IGNORECASE,
)


def _detect_policy_block(text: str) -> str | None:
    """识别需要硬拦截的高风险请求。"""
    if _OTHER_USER_DATA_PATTERNS.search(text):
        return "privacy_other_user"
    if _INJECTION_PATTERNS.search(text) and _UNRELATED_MATH_PATTERNS.search(text):
        return "unrelated_injection"
    return None


def _sanitize_input(text: str, max_len: int = 2048) -> str:
    """净化用户输入，防止提示词注入：转义花括号、检测注入模式、过滤伪造引用格式。"""
    text = text[:max_len]
    # 转义 Python str.format() 花括号，防止格式字符串注入
    text = text.replace("{", "{{").replace("}", "}}")
    if _INJECTION_PATTERNS.search(text):
        logger.warning("[analysis] 检测到疑似注入内容，已过滤")
        text = _INJECTION_PATTERNS.sub("[内容已过滤]", text)
    # 过滤伪造知识库 / 系统消息格式
    if _FAKE_REFERENCE_PATTERNS.search(text):
        logger.warning("[analysis] 检测到伪造引用/系统消息格式，已清除")
        text = _FAKE_REFERENCE_PATTERNS.sub("", text)
    # 过滤伪造规则块，避免“[参考1]\n新规则：...”这类内容继续影响模型
    if _FAKE_DIRECTIVE_PATTERNS.search(text):
        logger.warning("[analysis] 检测到伪造规则块，已清除")
        text = _FAKE_DIRECTIVE_PATTERNS.sub("", text)
    return text.strip()


class AnalysisResult(BaseModel):
    emotion:        Literal["calm", "excited", "angry"] = Field(description="情绪类型")
    emotion_polarity: Literal["positive", "negative", "neutral"] = Field(
        default="neutral", description="情绪极性：positive=正向(赞美/兴奋), negative=负向(不满/投诉), neutral=中性(calm时固定)"
    )
    emotion_score:  float = Field(description="情绪置信度 0~1", ge=0.0, le=1.0)
    emotion_reason: str   = Field(description="情绪判断理由，最多5个字")
    intents:        list[Literal["order", "knowledge", "chitchat", "unknown"]] = Field(
        description="意图列表，通常1个，order+knowledge同时时为2个",
        default_factory=lambda: ["unknown"],
    )
    intent_score:   float = Field(description="主意图置信度 0~1", ge=0.0, le=1.0)
    intent_reason:  str   = Field(description="意图判断理由，最多5个字")
    wants_purchase_link: bool = Field(
        default=False,
        description="本轮用户是否在表达明确下单/购买/索要购买入口意图（语义判断，由 LLM 输出）",
    )
    slots:          dict[str, Any] = Field(default_factory=dict, description="提取的槽位信息")


# ── Analysis 语义缓存（进程内，embedding 相似度匹配）──────────
import httpx
import numpy as np

# 模块级异步 httpx 客户端（连接池复用，不阻塞事件循环）
_embedding_http_client: httpx.AsyncClient | None = None


def _get_embedding_client() -> httpx.AsyncClient:
    """获取或创建 embedding 专用的异步 HTTP 客户端。"""
    global _embedding_http_client
    if _embedding_http_client is None or _embedding_http_client.is_closed:
        _embedding_http_client = httpx.AsyncClient(
            timeout=3.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _embedding_http_client


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """计算两个向量的余弦相似度。"""
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(dot / norm) if norm > 0 else 0.0


async def _get_embedding(text: str) -> np.ndarray | None:
    """异步获取文本 embedding 向量（带进程内缓存，不阻塞事件循环）。"""
    cached = _embedding_cache.get(text)
    if cached is not None:
        return cached
    try:
        from agent_framework.config.settings import get_settings
        s = get_settings()
        client = _get_embedding_client()
        resp = await client.post(
            f"{s.ollama_base_url}/api/embeddings",
            json={"model": s.ollama_embedding_model, "prompt": text},
        )
        if resp.status_code == 200:
            vec = np.array(resp.json()["embedding"], dtype=np.float32)
            _embedding_cache[text] = vec
            # 简单淘汰：超过上限时清掉最早的一半
            if len(_embedding_cache) > 1000:
                keys = list(_embedding_cache.keys())
                for k in keys[:500]:
                    del _embedding_cache[k]
            return vec
    except Exception as e:
        logger.warning("[analysis_cache] embedding 获取失败: %s", e)
    return None


# 进程内 embedding 缓存（相同文本 embedding 是确定性的）
_embedding_cache: dict[str, np.ndarray] = {}


class _AnalysisCache:
    """
    进程内 analysis 语义缓存。

    用 embedding 余弦相似度匹配，"多少钱" 和 "价格是多少" 可命中同一条缓存。
    精确匹配优先（零开销），不命中时再走 embedding 相似度。
    """

    def __init__(self, max_size: int = 500, ttl: int = 3600, similarity_threshold: float = 0.95):
        # 精确匹配索引
        self._exact: dict[str, tuple[float, AnalysisResult]] = {}
        # 语义匹配索引：[(timestamp, text, embedding, result), ...]
        self._semantic: list[tuple[float, str, np.ndarray, AnalysisResult]] = []
        self._max_size = max_size
        self._ttl = ttl
        self._threshold = similarity_threshold

    async def get(self, key: str) -> tuple[AnalysisResult | None, str]:
        """
        查询缓存（异步）。返回 (result, hit_type)。
        hit_type: "exact" / "semantic" / "miss"
        """
        now = time.time()

        # 1. 精确匹配（零开销）
        entry = self._exact.get(key)
        if entry is not None:
            ts, result = entry
            if now - ts <= self._ttl:
                return result, "exact"
            del self._exact[key]

        # 2. 语义匹配（异步 embedding，不阻塞事件循环）
        query_vec = await _get_embedding(key)
        if query_vec is None:
            return None, "miss"

        best_result = None
        best_score = 0.0
        expired_indices = []

        for i, (ts, text, vec, result) in enumerate(self._semantic):
            if now - ts > self._ttl:
                expired_indices.append(i)
                continue
            score = _cosine_similarity(query_vec, vec)
            if score > best_score:
                best_score = score
                best_result = result

        # 清理过期条目
        for i in reversed(expired_indices):
            self._semantic.pop(i)

        if best_score >= self._threshold and best_result is not None:
            # 语义命中：同时写入精确索引，下次同样的输入直接精确命中
            self._exact[key] = (now, best_result)
            return best_result, "semantic"

        return None, "miss"

    async def put(self, key: str, result: AnalysisResult) -> None:
        """写入缓存（精确 + 语义双写，异步）。"""
        now = time.time()

        # 精确索引
        if len(self._exact) >= self._max_size:
            sorted_keys = sorted(self._exact, key=lambda k: self._exact[k][0])
            for k in sorted_keys[:self._max_size // 5]:
                del self._exact[k]
        self._exact[key] = (now, result)

        # 语义索引
        vec = await _get_embedding(key)
        if vec is not None:
            if len(self._semantic) >= self._max_size:
                self._semantic = self._semantic[self._max_size // 5:]
            self._semantic.append((now, key, vec, result))


_analysis_cache = _AnalysisCache()


@lru_cache(maxsize=1)
def _build_llm():
    """根据 analysis_provider 配置创建模型实例，强制结构化输出。"""
    from agent_framework.config.settings import get_settings
    s = get_settings()

    if s.analysis_provider == "ollama":
        from langchain_ollama import ChatOllama
        llm = ChatOllama(
            model=s.analysis_model,
            base_url=s.ollama_base_url,
            temperature=s.analysis_temperature,
        )
        logger.info("[analysis] 使用 Ollama 本地模型: %s @ %s", s.analysis_model, s.ollama_base_url)
    else:
        from langchain_openai import ChatOpenAI
        extra_body = {"enable_thinking": False}  # 关闭 thinking 防止 reasoning tokens 吃光 max_tokens
        # 配置异步 httpx 客户端，增大连接池上限以支撑高并发
        async_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=80),
            timeout=30.0,
        )
        llm = ChatOpenAI(
            model=s.analysis_model,
            api_key=s.effective_api_key,
            base_url=s.openai_base_url,
            temperature=s.analysis_temperature,
            max_tokens=512,
            http_async_client=async_client,
            **({"extra_body": extra_body} if extra_body else {}),
        )
        logger.info("[analysis] 使用 OpenAI 兼容模型: %s", s.analysis_model)

    return llm.with_structured_output(AnalysisResult)


async def _notify_crm_human_transfer(
    session_id: str,
    user_id: str | None,
    user_input: str,
    transfer_reason: str,
    emotion: str | None,
    intents: list[str] | None = None,
) -> None:
    """
    通知 CRM / 外部系统：需要人工介入。
    配置 crm_human_transfer_webhook_url 时 POST JSON；否则仅打结构化日志。
    """
    payload = {
        "event": "human_transfer_requested",
        "session_id": session_id,
        "user_id": user_id,
        "transfer_reason": transfer_reason,
        "emotion": emotion,
        "intents": intents or [],
        "user_message_preview": user_input[:500],
    }
    logger.info("[crm_transfer] %s", json.dumps(payload, ensure_ascii=False))

    from agent_framework.config.settings import get_settings

    url = (get_settings().crm_human_transfer_webhook_url or "").strip()
    if not url:
        return
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=5.0)
            if resp.status_code >= 400:
                logger.warning("[crm_transfer] webhook HTTP %s body=%s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("[crm_transfer] webhook 失败: %s", e)


async def _prefetch_rag(user_input: str, collections: list[str]) -> list[dict]:
    """
    并行预取 RAG 检索结果（与 LLM 分析同时进行）。
    失败时返回空列表，不影响主流程。
    """
    try:
        from agents.customer_service.nodes.rag import _get_retriever, _build_query, _rrf_merge
        query = _build_query(user_input, {})

        tasks = [
            asyncio.to_thread(_get_retriever(col).retrieve, query, 5)
            for col in collections
        ]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        valid = [r for r in all_results if isinstance(r, list)]

        if not valid:
            return []
        if len(valid) == 1:
            return [
                {"content": r.content, "score": round(r.score, 4), "metadata": r.metadata}
                for r in valid[0] if r.score > 0
            ][:8]
        return _rrf_merge(valid)
    except Exception as e:
        logger.warning("[analysis] RAG 预取失败，将在 rag_node 重试: %s", e)
        return []


def _merge_slots(accumulated: dict, new_slots: dict, intent: str) -> dict:
    """将本轮 intent_slots 合并进 accumulated_slots，去重。"""
    result = {
        "order_ids":     list(set(accumulated.get("order_ids", []))),
        "tracking_nos":  list(set(accumulated.get("tracking_nos", []))),
        "product_names": list(set(accumulated.get("product_names", []))),
        "issue_types":   list(set(accumulated.get("issue_types", []))),
        "resolved":      list(accumulated.get("resolved", [])),
        "unresolved":    list(accumulated.get("unresolved", [])),
        "profile":       dict(accumulated.get("profile", {})),
    }
    if new_slots.get("order_id"):
        result["order_ids"] = list(set(result["order_ids"] + [new_slots["order_id"]]))
    if new_slots.get("tracking_no"):
        result["tracking_nos"] = list(set(result["tracking_nos"] + [new_slots["tracking_no"]]))
    if new_slots.get("product_name"):
        result["product_names"] = list(set(result["product_names"] + [new_slots["product_name"]]))
    if intent and intent not in ("unknown", "chitchat"):
        result["issue_types"] = list(set(result["issue_types"] + [intent]))

    return result


async def analysis_node(state: CustomerServiceState) -> dict[str, Any]:
    """
    情绪+意图合并分析节点。

    并行执行：
      - LLM 分析（情绪 + 意图 + 槽位）
      - RAG 预取（knowledge/unknown 意图概率高，提前检索）

    输入:  user_input, session_id, messages, knowledge_collections
    输出:  emotion/intent 相关字段 + prefetched_chunks（供 rag_node 直接复用）
    """
    t0         = time.time()
    raw_user_input = state.get("user_input", "")
    policy_block = _detect_policy_block(raw_user_input)
    user_input  = _sanitize_input(raw_user_input)
    session_id  = state.get("session_id", "default")
    messages    = state.get("messages") or []
    persona_cfg = state.get("persona_config") or {}
    collections = state.get("knowledge_collections") or persona_cfg.get("collections") or _DEFAULT_COLLECTIONS
    if isinstance(collections, str):
        collections = [collections]

    # CRM 传入的历史画像作为本会话 accumulated_slots["profile"] 的初始值
    # 仅在 accumulated_slots 尚无 profile 时注入（避免覆盖同会话已累积的新数据）
    crm_profile = (state.get("customer_profile") or {}).get("profile") or {}
    existing_accumulated = dict(state.get("accumulated_slots") or {})
    if crm_profile and not existing_accumulated.get("profile"):
        existing_accumulated.setdefault("profile", {}).update(crm_profile)
        logger.debug("[analysis] 从 customer_profile 注入历史画像: %s", list(crm_profile.keys()))

    if policy_block == "privacy_other_user":
        debug_info = dict(state.get("debug_info") or {})
        debug_info["analysis_ms"] = round((time.time() - t0) * 1000)
        debug_info["analysis_cached"] = False
        debug_info["rag_prefetched"] = False
        debug_info["policy_block"] = policy_block
        scene, scene_source = _resolve_scene(state)
        journey_stage = _resolve_journey_stage(state, scene)
        return {
            "emotion": "calm",
            "emotion_polarity": "neutral",
            "emotion_score": 1.0,
            "emotion_reason": "越权请求",
            "need_human_transfer": False,
            "transfer_reason": None,
            "intents": ["unknown"],
            "intent": "unknown",
            "intent_score": 1.0,
            "intent_reason": "越权请求",
            "intent_slots": {},
            "accumulated_slots": existing_accumulated,
            "retrieved_chunks": [],
            "retrieval_query": "",
            "scene": scene,
            "scene_source": scene_source,
            "journey_stage": journey_stage,
            "wants_purchase_link": False,
            "debug_info": debug_info,
        }

    if policy_block == "unrelated_injection":
        debug_info = dict(state.get("debug_info") or {})
        debug_info["analysis_ms"] = round((time.time() - t0) * 1000)
        debug_info["analysis_cached"] = False
        debug_info["rag_prefetched"] = False
        debug_info["policy_block"] = policy_block
        scene, scene_source = _resolve_scene(state)
        journey_stage = _resolve_journey_stage(state, scene)
        return {
            "emotion": "calm",
            "emotion_polarity": "neutral",
            "emotion_score": 1.0,
            "emotion_reason": "注入拦截",
            "need_human_transfer": False,
            "transfer_reason": None,
            "intents": ["unknown"],
            "intent": "unknown",
            "intent_score": 1.0,
            "intent_reason": "注入拦截",
            "intent_slots": {},
            "accumulated_slots": existing_accumulated,
            "retrieved_chunks": [],
            "retrieval_query": "",
            "scene": scene,
            "scene_source": scene_source,
            "journey_stage": journey_stage,
            "wants_purchase_link": False,
            "debug_info": debug_info,
        }

    # ── 净化后无可分析文本（仅空格/换行等）：跳过 LLM、embedding 与 RAG 预取 ──
    if not user_input:
        debug_info = dict(state.get("debug_info") or {})
        debug_info["analysis_ms"] = round((time.time() - t0) * 1000)
        debug_info["analysis_cached"] = False
        debug_info["rag_prefetched"] = False
        debug_info["blank_user_message"] = True
        scene, scene_source = _resolve_scene(state)
        journey_stage = _resolve_journey_stage(state, scene)
        return {
            "emotion": "calm",
            "emotion_polarity": "neutral",
            "emotion_score": 1.0,
            "emotion_reason": "无文本",
            "need_human_transfer": False,
            "transfer_reason": None,
            "intents": ["unknown"],
            "intent": "unknown",
            "intent_score": 1.0,
            "intent_reason": "无文本",
            "intent_slots": {},
            "accumulated_slots": existing_accumulated,
            "retrieved_chunks": [],
            "retrieval_query": "",
            "scene": scene,
            "scene_source": scene_source,
            "journey_stage": journey_stage,
            "field_updates": None,
            "wants_purchase_link": False,
            "debug_info": debug_info,
        }

    # ── 快速购买信号（如“扣1/回1”）：不走 LLM，直接按购买意向处理 ─────────
    if _QUICK_BUY_SIGNAL_RE.match(user_input):
        prefetched_chunks = await _prefetch_rag(user_input, collections)
        if isinstance(prefetched_chunks, Exception) or not isinstance(prefetched_chunks, list):
            prefetched_chunks = []
        debug_info = dict(state.get("debug_info") or {})
        debug_info["analysis_ms"] = round((time.time() - t0) * 1000)
        debug_info["analysis_cached"] = False
        debug_info["rag_prefetched"] = len(prefetched_chunks) > 0
        debug_info["quick_buy_signal"] = True
        scene, scene_source = _resolve_scene(state)
        journey_stage = _resolve_journey_stage(state, scene)
        field_updates: dict[str, Any] = {}
        crm_updates = _detect_crm_field_updates(user_input, scene)
        field_updates.update(crm_updates)
        field_updates = field_updates or None
        return {
            "emotion": "calm",
            "emotion_polarity": "neutral",
            "emotion_score": 0.95,
            "emotion_reason": "购买口令",
            "need_human_transfer": False,
            "transfer_reason": None,
            "intents": ["knowledge"],
            "intent": "knowledge",
            "intent_score": 0.98,
            "intent_reason": "购买信号",
            "intent_slots": {},
            "accumulated_slots": existing_accumulated,
            "retrieved_chunks": prefetched_chunks,
            "retrieval_query": user_input,
            "scene": scene,
            "scene_source": scene_source,
            "journey_stage": journey_stage,
            "field_updates": field_updates,
            "wants_purchase_link": False,
            "debug_info": debug_info,
        }

    # ── 售后类（退货/退款/投诉）：不调 LLM，静默转 CRM，不向客户生成话术 ─────────
    if _after_sales_hit(user_input):
        scene, scene_source = _resolve_scene(state)
        journey_stage = _resolve_journey_stage(state, scene)
        field_updates: dict[str, Any] = {}
        crm_updates = _detect_crm_field_updates(user_input, scene)
        field_updates.update(crm_updates)
        field_updates = field_updates or None
        await _notify_crm_human_transfer(
            session_id=session_id,
            user_id=state.get("user_id"),
            user_input=user_input,
            transfer_reason="after_sales",
            emotion="calm",
            intents=["order"],
        )
        debug_info = dict(state.get("debug_info") or {})
        debug_info["analysis_ms"] = round((time.time() - t0) * 1000)
        debug_info["analysis_cached"] = False
        debug_info["rag_prefetched"] = False
        debug_info["after_sales_fast_path"] = True
        return {
            "emotion": "calm",
            "emotion_polarity": "neutral",
            "emotion_score": 1.0,
            "emotion_reason": "售后类关键词（退货/退款/投诉），静默转人工",
            "need_human_transfer": True,
            "transfer_reason": "after_sales",
            "intents": ["order"],
            "intent": "order",
            "intent_score": 1.0,
            "intent_reason": "售后类静默转人工",
            "intent_slots": {},
            "accumulated_slots": existing_accumulated,
            "retrieved_chunks": [],
            "retrieval_query": "",
            "scene": scene,
            "scene_source": scene_source,
            "journey_stage": journey_stage,
            "field_updates": field_updates,
            "wants_purchase_link": False,
            "debug_info": debug_info,
        }

    # ── 人工处理完毕转回 AI：隔离历史情绪上下文 ─────────────────
    human_handled = state.get("human_handled", False)

    # 格式化最近 3 轮对话历史
    history_msgs = [m for m in messages if hasattr(m, "type")]
    recent = history_msgs[-6:] if len(history_msgs) > 6 else history_msgs
    history_lines = [
        f"{'用户' if m.type == 'human' else '客服'}：{m.content[:100].replace(chr(10), ' ').replace(chr(13), ' ')}"
        for m in recent
    ]
    if human_handled:
        # 在历史末尾插入隔离标记，告知 LLM 之前的情绪已由人工处理完毕
        history_lines.append("【系统提示】以上对话中的情绪问题已由人工客服处理完毕，客户情绪已平复，请按正常状态继续服务。")
        logger.info("[analysis] human_handled=True，已注入历史隔离标记: session=%s", session_id)
    history = "\n".join(history_lines) or "（无历史）"

    prompt = ANALYSIS_USER.format(history=history, user_input=user_input)

    # ── 缓存查询（有槽位的不走缓存，含具体订单号等业务数据）───
    cached, hit_type = await _analysis_cache.get(user_input)
    if cached and not cached.slots:
        logger.info("[analysis] 缓存命中(%s): '%s' → intents=%s emotion=%s",
                    hit_type, user_input[:20], cached.intents, cached.emotion)
        prefetched_chunks = await _prefetch_rag(user_input, collections)
        if isinstance(prefetched_chunks, Exception) or not isinstance(prefetched_chunks, list):
            prefetched_chunks = []

        intents_list = cached.intents or ["unknown"]
        (
            emo,
            ep,
            es,
            er,
            intents_list,
            isc,
            ir,
        ) = _apply_benign_emotion_overrides(
            user_input,
            cached.emotion,
            cached.emotion_polarity,
            cached.emotion_score,
            cached.emotion_reason,
            intents_list,
            cached.intent_score,
            cached.intent_reason,
        )
        intents_list = _augment_mixed_intents(user_input, intents_list)
        primary_intent = intents_list[0]
        need_transfer, transfer_reason = _resolve_transfer_reason(
            user_input,
            human_handled,
            emo,
            ep,
            es,
        )
        if need_transfer and transfer_reason:
            await _notify_crm_human_transfer(
                session_id=session_id,
                user_id=state.get("user_id"),
                user_input=user_input,
                transfer_reason=transfer_reason,
                emotion=emo,
                intents=intents_list,
            )

        accumulated_slots = _merge_slots(existing_accumulated, cached.slots or {}, primary_intent)
        debug_info = dict(state.get("debug_info") or {})
        debug_info["analysis_ms"]     = round((time.time() - t0) * 1000)
        debug_info["analysis_cached"] = hit_type   # "exact" 或 "semantic"
        debug_info["rag_prefetched"]  = len(prefetched_chunks) > 0
        scene, scene_source = _resolve_scene(state)
        journey_stage = _resolve_journey_stage(state, scene)
        if scene_source == "inferred_summary":
            debug_info["scene_inference"] = {
                "source": scene_source,
                "scene": scene,
                "journey_stage": journey_stage,
            }
        field_updates = {}
        crm_updates = _detect_crm_field_updates(user_input, scene)
        field_updates.update(crm_updates)
        field_updates = field_updates or None
        return {
            "emotion": emo, "emotion_polarity": ep,
            "emotion_score": es,
            "emotion_reason": er, "need_human_transfer": need_transfer,
            "transfer_reason": transfer_reason,
            "intents": intents_list, "intent": primary_intent,
            "intent_score": isc, "intent_reason": ir,
            "intent_slots": cached.slots or {}, "accumulated_slots": accumulated_slots,
            "retrieved_chunks": prefetched_chunks, "retrieval_query": user_input,
            "scene": scene, "scene_source": scene_source,
            "journey_stage": journey_stage,
            "field_updates": field_updates,
            "wants_purchase_link": False,
            "debug_info": debug_info,
        }

    # ── 并行：LLM 分析 + RAG 预取 ────────────────────────
    from langchain_core.messages import HumanMessage, SystemMessage
    llm_task = _build_llm().ainvoke([
        SystemMessage(content=ANALYSIS_SYSTEM),
        HumanMessage(content=prompt),
    ])
    rag_task = _prefetch_rag(user_input, collections)

    llm_result, prefetched_chunks = await asyncio.gather(
        llm_task, rag_task, return_exceptions=True
    )

    # 处理 LLM 结果
    if isinstance(llm_result, Exception):
        logger.warning("[analysis] LLM 分析失败，降级处理: %s", llm_result)
        emotion, emotion_polarity, emotion_score, emotion_reason = "calm", "neutral", 0.5, f"降级: {llm_result}"
        intents_list = ["knowledge"]
        intent_score, intent_reason = 0.5, f"降级: {llm_result}"
        slots = {}
        wants_purchase_link = False
    elif not isinstance(llm_result, AnalysisResult):
        logger.warning("[analysis] LLM 返回类型异常: %s，降级处理", type(llm_result))
        emotion, emotion_polarity, emotion_score, emotion_reason = "calm", "neutral", 0.5, "类型异常降级"
        intents_list = ["knowledge"]
        intent_score, intent_reason = 0.5, "类型异常降级"
        slots = {}
        wants_purchase_link = False
    else:
        emotion        = llm_result.emotion
        emotion_polarity = llm_result.emotion_polarity
        emotion_score  = llm_result.emotion_score
        emotion_reason = llm_result.emotion_reason
        intents_list   = llm_result.intents or ["unknown"]
        intent_score   = llm_result.intent_score
        intent_reason  = llm_result.intent_reason
        slots          = llm_result.slots or {}
        wants_purchase_link = bool(getattr(llm_result, "wants_purchase_link", False))
        (
            emotion,
            emotion_polarity,
            emotion_score,
            emotion_reason,
            intents_list,
            intent_score,
            intent_reason,
        ) = _apply_benign_emotion_overrides(
            user_input,
            emotion,
            emotion_polarity,
            emotion_score,
            emotion_reason,
            intents_list,
            intent_score,
            intent_reason,
        )
        intents_list = _augment_mixed_intents(user_input, intents_list)
        to_cache = (
            llm_result.model_copy(
                update={
                    "emotion": emotion,
                    "emotion_polarity": emotion_polarity,
                    "emotion_score": emotion_score,
                    "emotion_reason": emotion_reason,
                    "intents": intents_list,
                    "intent_score": intent_score,
                    "intent_reason": intent_reason,
                }
            )
            if _analysis_cache_should_store_corrected(user_input)
            else llm_result
        )
        await _analysis_cache.put(user_input, to_cache)

    # 主意图（取列表第一个，向后兼容 API 响应 / debug 接口）
    primary_intent = intents_list[0] if intents_list else "unknown"

    # 处理 RAG 预取结果
    if isinstance(prefetched_chunks, Exception) or not isinstance(prefetched_chunks, list):
        prefetched_chunks = []

    need_transfer, transfer_reason = _resolve_transfer_reason(
        user_input, human_handled, emotion, emotion_polarity, emotion_score,
    )
    if need_transfer and transfer_reason:
        await _notify_crm_human_transfer(
            session_id=session_id,
            user_id=state.get("user_id"),
            user_input=user_input,
            transfer_reason=transfer_reason,
            emotion=emotion,
            intents=intents_list,
        )

    # ── 累积槽位合并 ─────────────────────────────────────
    accumulated_slots = _merge_slots(
        existing_accumulated,
        slots,
        primary_intent,
    )

    debug_info = dict(state.get("debug_info") or {})
    debug_info["analysis_ms"]     = round((time.time() - t0) * 1000)
    debug_info["analysis_cached"] = False
    debug_info["rag_prefetched"]  = len(prefetched_chunks) > 0

    scene, scene_source = _resolve_scene(state)
    journey_stage = _resolve_journey_stage(state, scene)
    if scene_source == "inferred_summary":
        debug_info["scene_inference"] = {
            "source": scene_source,
            "scene": scene,
            "journey_stage": journey_stage,
        }

    logger.info(
        "[analysis] session=%s emotion=%s(%s/%.2f) intents=%s(%.2f) "
        "transfer=%s scene=%s(%s) prefetch=%d elapsed=%dms",
        session_id, emotion, emotion_polarity, emotion_score, intents_list, intent_score,
        need_transfer, scene, scene_source, len(prefetched_chunks), debug_info["analysis_ms"],
    )
    field_updates = {}
    crm_updates = _detect_crm_field_updates(user_input, scene)
    field_updates.update(crm_updates)
    field_updates = field_updates or None
    return {
        "emotion":              emotion,
        "emotion_polarity":     emotion_polarity,
        "emotion_score":        emotion_score,
        "emotion_reason":       emotion_reason,
        "need_human_transfer":  need_transfer,
        "transfer_reason":      transfer_reason,
        "intents":              intents_list,
        "intent":               primary_intent,
        "intent_score":         intent_score,
        "intent_reason":        intent_reason,
        "intent_slots":         slots,
        "accumulated_slots":    accumulated_slots,
        "retrieved_chunks":     prefetched_chunks,
        "retrieval_query":      user_input,
        "scene":                scene,
        "scene_source":         scene_source,
        "journey_stage":        journey_stage,
        "field_updates":        field_updates,
        "wants_purchase_link":  wants_purchase_link,
        "debug_info":           debug_info,
    }
