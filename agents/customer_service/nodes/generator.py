"""
回复生成节点（异步版）

调用 qwen-flash（通义千问）生成最终回复。
支持摘要缓冲混合记忆：最近 K_RECENT 轮直接传递，更早的历史压缩为摘要。
"""
from __future__ import annotations
from functools import lru_cache

import asyncio
import hashlib
import time
import logging
import random
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.customer_service.prompts import REPLY_CHITCHAT_INTENT_CLARIFY, REPLY_SHORT_ACK
from agents.customer_service.state import CustomerServiceState

logger = logging.getLogger(__name__)

# 直接传递最近 k 轮（每轮 = 1条 Human + 1条 AI = 2条消息）
K_RECENT = 6
K_MESSAGES = K_RECENT * 2


# ── Generator 输出缓存（进程内，intent+上下文哈希匹配）──────────────
# 仅缓存 chitchat 和无动态数据的 knowledge 回复
_CACHEABLE_INTENTS = {"chitchat", "unknown", "knowledge"}

class _GeneratorCache:
    """
    Generator 输出缓存。

    key = intent + context_text 的 MD5 哈希。
    仅缓存无槽位（无订单号等动态数据）的 chitchat/knowledge 回复。
    """

    def __init__(self, max_size: int = 200, ttl: int = 1800):
        self._store: dict[str, tuple[float, str, list[str]]] = {}  # key → (ts, reply, parts)
        self._max_size = max_size
        self._ttl = ttl

    @staticmethod
    def make_key(intent: str, context_text: str, user_input: str, system_prompt: str = "") -> str:
        # 包含 system_prompt 前 200 字符（含 brand/role/业务规则），确保不同 bot 缓存隔离
        sp = system_prompt[:200] if system_prompt else ""
        raw = f"{intent}:{sp}:{context_text}:{user_input}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> tuple[str, list[str]] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, reply, parts = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return reply, parts

    def put(self, key: str, reply: str, parts: list[str]) -> None:
        if len(self._store) >= self._max_size:
            # 淘汰最旧的 20%
            sorted_keys = sorted(self._store, key=lambda k: self._store[k][0])
            for k in sorted_keys[:self._max_size // 5]:
                del self._store[k]
        self._store[key] = (time.time(), reply, parts)


_generator_cache = _GeneratorCache()


# ── 场景化结构化标签提取 ────────────────────────────────────────
# 匹配格式：<<DELTA:+10,STAGE:awaiting_reply_3>> 或 <<DELTA:-5,STAGE:awaiting_reply_5>>
# DELTA 取值 -30..+30；STAGE 仅 awaiting_reply_1 ~ awaiting_reply_5
_SCENE_META_RE = re.compile(
    r"<<\s*DELTA\s*:\s*(?P<delta>[+-]?\d+)\s*,\s*STAGE\s*:\s*(?P<stage>[a-z_][a-z0-9_]*)\s*>>",
    re.IGNORECASE,
)
_SCENE_META_FUZZY_RE = re.compile(
    r"<<[^>\n]{0,80}\bDELTA\b[^>\n]{0,120}\bSTAGE\b[^>\n]{0,120}>>",
    re.IGNORECASE,
)
_MINIAPP_LINK_RE = re.compile(r"#小程序://\S+")
_DEFAULT_MINIAPP_LINK = "#小程序://新肌秀New/IZwsn2JSuw5iNUJ"  # 向后兼容旧 bot 的兜底链接


def _get_purchase_link(state: dict) -> str:
    """从 state 的 business rules 中读取购买链接，兜底为默认链接。"""
    persona_config = state.get("persona_config") or {}
    business = persona_config.get("business") or {}
    links = business.get("purchase_links") or []
    return links[0] if links else _DEFAULT_MINIAPP_LINK
_PURCHASE_INTENT_RE = re.compile(
    r"(?:想买|购买|拍下|链接|怎么买|哪里买|在哪买|哪儿买|发(个)?链接|给我(发)?链接|我(要|想)买|来一瓶|回购|"
    r"想下单|要下单|怎么下单|哪里下单|在哪下单|在哪里下单|哪儿下单|去哪下单|到哪下单|"
    r"(?:麻烦|请|帮|替|给|叫)\s*(?:我)?\s*下单|(?:你|您|智能体)(?:帮|给|替)?(?:我)?\s*下单|代\s*下单|(?:帮我|替我|给我)\s*下单|"
    r"扣\s*1|回\s*1|回复\s*1|打\s*1)",
    re.IGNORECASE,
)
_EXPLICIT_LINK_REQUEST_RE = re.compile(
    r"(?:发(个)?链接|给我(发)?链接|链接发我|怎么买|哪里买|在哪买|哪儿买|购买链接|下单链接|"
    r"怎么下单|哪里下单|在哪下单|在哪里下单|哪儿下单|去哪下单|到哪下单|"
    r"(?:麻烦|请|帮|替)\s*(?:我)?\s*下单|(?:你|您|智能体)\s*(?:帮|给)?(?:我)?\s*下单|代\s*下单|(?:帮我|替我|给我)\s*下单|"
    r"扣\s*1|回\s*1|回复\s*1|打\s*1)",
    re.IGNORECASE,
)
_POST_ORDER_CONTEXT_RE = re.compile(
    r"(?:我下单了|已下单|已经下单|下单后|发货|物流|多久到|什么时候到|几天到|运费险|退货|退款|售后|订单)",
    re.IGNORECASE,
)
_POST_TRIAL_POSITIVE_RE = re.compile(
    r"(?:用(?:了|过|起来)|试(?:了|过|用)|洗(?:了|过|起来)).{0,8}(?:还行|可以|不错|挺好|很好|蛮好|满意)|"
    r"(?:还行|可以|不错|挺好|很好|蛮好|满意).{0,8}(?:用(?:了|过|起来)|试(?:了|过|用)|洗(?:了|过|起来))",
    re.IGNORECASE,
)
# 未体验客户跟进：本轮已识别「体验后反馈」且评价偏中性/好评（不必同时出现「用了+还行」紧挨）
_TRIAL_EVAL_NEUTRAL_OR_POSITIVE_RE = re.compile(
    r"(?:还行|还可以|不错|挺好|很好|蛮好|满意|好用|喜欢|挺喜欢|能接受|没问题|挺舒服|挺清爽|香(?:味)?(?:不错|好|喜欢)?|"
    r"泡沫(?:丰富|多|细)?|清洁力|洗完|肤感|一般般|一般|凑合|无功无过|回购|会再买)",
    re.IGNORECASE,
)
_TRIAL_EVAL_STRONG_NEGATIVE_RE = re.compile(
    r"(?:过敏|红肿|发痒|太难用|很失望|垃圾|难用|不要用|刺激|疼|辣眼睛|骗人|假货|"
    r"退货|退款|投诉|恶心|毁容|烂脸)",
    re.IGNORECASE,
)
# 体验层面的轻量负反馈：不算强负但带使用体验抱怨，此时补链会显得生硬推销
# 命中就不自动补链，先把体验问题答清楚再说
_TRIAL_EVAL_MILD_COMPLAINT_RE = re.compile(
    r"(?:干(?:了|的|点|些)?|紧绷|有点紧|拔干|发涩|涩(?:了|的)?|刺(?:了|的|眼)?|"
    r"味道(?:怪|重|冲|不(?:喜欢|好闻)|难闻)|香味(?:太(?:浓|重)|刺鼻|冲)|刺鼻|"
    r"黏(?:腻|糊)?|油(?:腻|乎)|滑(?:溜|腻)|不(?:起泡|够泡)|泡沫(?:少|一般)|"
    r"不够(?:清爽|滋润)|偏(?:碱|酸|干))",
    re.IGNORECASE,
)
# 模型口头承诺发链但未带 #小程序:// 时剔除空话，避免「链接发您」却无 #小程序://
_HOLLOW_LINK_PROMISE_RE = re.compile(
    r"(?:收到[，,]\s*)?链接发您(?=[：:\s]|$)[：:\s]*|"
    r"(?:收到[，,]\s*)?(?:下单|购买)?链接[：:\s]*发您(?=[：:\s]|$)[：:\s]*|"
    r"(?:这就把|马上把|这边(?:先)?把)(?:下单|购买)?链接(?:发您|给您|发给你)|"
    r"(?:这就|马上)(?:给您|给你)(?:发|发一下)?(?:个)?(?:下单|购买)?链接|"
    r"(?:给您|给你)(?:发|发一下)(?:个)?(?:下单|购买)?链接|"
    r"点(?:这里|我)(?:下单|购买)",
    re.IGNORECASE,
)
# 模型照搬系统提示里的「同一会话链接最多×次」：无 #小程序 时不应输出给客户
_LINK_CNT = r"(?:两|二|2|两次|二次)"
_LINK_SESSION_QUOTA_RE = re.compile(
    rf"同一会话[中里内]?链接最多(?:能发|可发|发送)?\s*{_LINK_CNT}次[哦噢呀啊～\s]*|"
    rf"同一会话[中里内]?链接最多.{{0,20}}?{_LINK_CNT}次[哦噢呀啊～\s]*|"
    rf"同一.{{0,25}}?会话.{{0,25}}?链接.{{0,25}}?{_LINK_CNT}次[哦噢呀啊～\s]*|"
    rf"链接最多(?:能发|可发|发送)?\s*{_LINK_CNT}次[哦噢呀啊～\s]*|"
    rf"链接最多.{{0,16}}?{_LINK_CNT}次[哦噢呀啊～\s]*",
    re.IGNORECASE | re.DOTALL,
)
_SOFT_HANDOFF_USER_RE = re.compile(r"(?:转人工|人工客服|真人客服|换人|换个客服|找人工|找个真人|换客服)")
_HANDOFF_PROMISE_RE = re.compile(
    r"(?:马上|立刻|这就|我现在)?(?:帮您|给您)?(?:转接|转到|联系)(?:人工|人工客服|真人客服)|"
    r"请(?:稍等|等待).{0,8}(?:分钟|min)|"
    r"等待(?:1|2|3|一|二|三).{0,4}(?:分钟|min)",
    re.IGNORECASE,
)
_SOFT_HANDOFF_CLARIFY_REPLY = "可以的呢～我先帮您确认下，您这边是想处理订单问题，还是产品使用问题？我马上给您对接对应同事。"
_SPECIAL_POP_QUESTION_RE = re.compile(r"(?:儿童|小孩|小朋友|婴儿|婴幼儿|孕妇|全年龄段)")
_OUTDATED_SPECIAL_POP_RE = re.compile(
    r"(?:没有专项结论|建议(?:还是)?先咨询医生(?:比较)?稳妥|谨慎使用并咨询)",
    re.IGNORECASE,
)
_SPECIAL_POP_POLICY = "不建议6岁以下儿童、婴幼儿或孕妇使用；如确需使用，请先咨询医生。"
def _whole_message_is_lone_one(user_input: str) -> bool:
    """整段（含首尾空白）净化后仅为数字 1。该信号歧义较大，按"先澄清客户目的"处理。"""
    return bool(re.fullmatch(r"\s*1\s*", user_input or ""))


def _user_explicit_wants_link(user_input: str, semantic_signal: bool | None = None) -> bool:
    """
    用户本轮是否明确要购买/要链接（用于判断是否允许口头承诺+补链，或是否应剔除空话）。

    优先级：
      1. analysis_node 的 LLM 语义判断（state['wants_purchase_link']）—— 覆盖各种口语变体
      2. 正则兜底 —— 保护 LLM 偶发误判，避免 false negative
    """
    if semantic_signal is True:
        return True
    u = user_input or ""
    return bool(_EXPLICIT_LINK_REQUEST_RE.search(u) or _PURCHASE_INTENT_RE.search(u))


def _scrub_hollow_link_promises(reply_parts: list[str]) -> list[str]:
    """
    从非链接气泡中剔除「这就把链接发您」类空头表述；含小程序链接的气泡原样保留。
    （用户未明确购买时模型仍可能乱承诺，统一清掉；已拆出的链接行不受影响。）
    """
    out: list[str] = []
    for part in reply_parts:
        if _MINIAPP_LINK_RE.search(part or ""):
            out.append(part)
            continue
        text = part or ""
        for _ in range(4):
            new_t = _HOLLOW_LINK_PROMISE_RE.sub("", text)
            if new_t == text:
                break
            text = new_t
        text = re.sub(r"[，,]{2,}", "，", text)
        text = re.sub(r"\s+", " ", text).strip(" ，,。.;；\n\t")
        if text:
            out.append(text)
    return out if out else [REPLY_SHORT_ACK]


def _scrub_part_after_regexes(text: str, *patterns: re.Pattern[str]) -> str:
    t = text or ""
    for pat in patterns:
        for _ in range(4):
            nt = pat.sub("", t)
            if nt == t:
                break
            t = nt
    t = re.sub(r"[，,]{2,}", "，", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,。.;；\n\t")
    return t


def _scrub_session_link_quota_copy(reply_parts: list[str], user_input: str) -> list[str]:
    """无 #小程序 时去掉「同一会话链接最多×次」等内部频控复述（与是否口头要链无关）。"""
    _ = user_input
    if any(_MINIAPP_LINK_RE.search(p or "") for p in reply_parts):
        return reply_parts
    out: list[str] = []
    for part in reply_parts:
        if _MINIAPP_LINK_RE.search(part or ""):
            out.append(part)
            continue
        t = _scrub_part_after_regexes(part, _LINK_SESSION_QUOTA_RE)
        if t:
            out.append(t)
    return out


def _only_generic_closing_message(text: str) -> bool:
    """整段仅为客套收尾、无实质业务内容。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return True
    patterns = (
        r"^如果您还有其他疑问[，,]\s*随时找我[!！～\s]*$",
        r"^随时找我[!！～\s]*$",
        r"^有问题随时告诉我[🌹\s！!～。]*$",
        r"^还有什么(?:可以帮您|帮您的)?[吗?？!！。～\s]*$",
    )
    return any(re.fullmatch(p, t) for p in patterns)


def _maybe_clarify_weak_chitchat_reply(
    reply_parts: list[str],
    intent: str | None,
    user_input: str,
    brand: str = "",
    role: str = "专业客服",
) -> list[str]:
    """chitchat/unknown 且无购买/要链信号、无小程序链接时，避免只回客套句或空内容。"""
    if (intent or "") not in ("chitchat", "unknown"):
        return reply_parts
    if _user_explicit_wants_link(user_input or ""):
        return reply_parts
    if any(_MINIAPP_LINK_RE.search(p or "") for p in reply_parts):
        return reply_parts
    joined = "\n".join((p or "").strip() for p in reply_parts if (p or "").strip())
    if not joined.strip() or _only_generic_closing_message(joined):
        return [REPLY_CHITCHAT_INTENT_CLARIFY.format(brand=brand, role=role)]
    return reply_parts


_LINK_ACK_ONLY_RE = re.compile(
    r"^(?:好(?:的)?|收到|明白(?:了)?|嗯(?:嗯)?|ok|okay)[～~!！。,，\s]*$",
    re.IGNORECASE,
)


def _ensure_explicit_purchase_reply_substance(
    reply_parts: list[str],
    user_input: str,
    allow_links: bool,
    default_link: str = _DEFAULT_MINIAPP_LINK,
) -> list[str]:
    """
    用户明确购买/代下单诉求时，禁止整轮只剩「好的」等弱确认。
    （regex 漏匹配或前文清洗把正文洗掉时的最后一道兜底）
    """
    if not _user_explicit_wants_link(user_input or ""):
        return reply_parts
    joined = "\n".join((p or "").strip() for p in reply_parts if (p or "").strip())
    lone = re.sub(r"\s+", " ", joined.strip())
    is_weak = (
        not lone
        or bool(_LINK_ACK_ONLY_RE.fullmatch(lone))
        or lone in ("好的", "好", "嗯", "嗯嗯", "行", "OK", "ok", "Okay")
    )
    if not is_weak:
        return reply_parts
    has_link = any(_MINIAPP_LINK_RE.search(p or "") for p in reply_parts)
    body = (
        "我这边没法替您直接代下单哦，需要您本人在小程序里选好规格并完成支付～"
        "我把购买入口发您。"
    )
    if allow_links and not has_link:
        return [body, default_link]
    if allow_links and has_link:
        return _upgrade_purchase_ack_when_linked(reply_parts, user_input)
    return [body]


def _upgrade_purchase_ack_when_linked(reply_parts: list[str], user_input: str) -> list[str]:
    """
    购买信号命中且本轮有链接时，避免正文只剩「好的/收到」这类弱确认。
    将其升级为明确的下单引导确认语，提升可用性与一致性。
    """
    if not _user_explicit_wants_link(user_input or ""):
        return reply_parts
    if not any(_MINIAPP_LINK_RE.search(p or "") for p in reply_parts):
        return reply_parts
    out: list[str] = []
    for p in reply_parts:
        t = (p or "").strip()
        if not t:
            continue
        if _MINIAPP_LINK_RE.search(t):
            out.append(t)
            continue
        if _LINK_ACK_ONLY_RE.fullmatch(t):
            out.append("收到，这就把下单链接发您。")
        else:
            out.append(t)
    return out if out else ["收到，这就把下单链接发您。"]


# 工具未返回有效轨迹时，模型仍可能套用知识库「广州/时效」编造在途状态
_FANTASY_LOGISTICS_STATUS_RE = re.compile(
    r"(?:运输中|正在运输|在途|派送中|配送中|已揽收|已发出|从广州发出|"
    r"明天.{0,8}[到送达]|预计.{0,10}[到送达]|省外.{0,8}[天日到])",
)

# 未验证轨迹时替换幻觉回复，随机择一以降低机械感；{tn} 为展示用运单号
_UNVERIFIED_LOGISTICS_SAFE_TEMPLATES: tuple[str, ...] = (
    "我这边暂时查不到运单{tn}的实时物流轨迹，麻烦您核对一下单号是否完整正确，或稍后再试～",
    "目前系统里还没有运单{tn}的有效物流更新呢，您方便再确认单号有没有填对，或过一会儿再来问我～",
    "单号{tn}我这边暂时看不到在途信息哦，建议您核对无误后稍晚再查，具体以快递公司官方更新为准～",
    "抱歉呀，运单{tn}这边暂时没有查到实时轨迹，可能是单号有误或物流还没同步，您核实一下或稍后再试哈～",
    "我查了下运单{tn}，暂没有在途明细能给您确认；请您对照单号是否一致，或隔一段时间再查最新物流～",
    "这边没能查到运单{tn}的物流动态，麻烦您把单号发完整（或截图面单）我帮您对一下，也可以稍后再查查看～",
    "运单{tn}暂时查无在途记录哈，您先核对单号是否正确，若刚发货可能还未揽收，晚点再来看看～",
)


def _order_logistics_is_unverified(order_result: Any) -> bool:
    """工具侧无可靠物流轨迹（或订单查询失败），用于拦截在途/时效幻觉。"""
    if not order_result or not isinstance(order_result, dict):
        return True
    # 降级/失败：整单无有效数据，logistics 子块可能缺失
    if order_result.get("error") or order_result.get("fallback"):
        return True
    logistics = order_result.get("logistics")
    if logistics is None:
        return False
    if not isinstance(logistics, dict):
        return True
    if logistics.get("error"):
        return True
    msg = str(logistics.get("message") or "")
    if "未查询到" in msg or "有效物流" in msg:
        return True
    if logistics.get("_verified") is True:
        st = str(logistics.get("status") or "").strip()
        return not st
    st = str(logistics.get("status") or "").strip()
    return not st or st == "未知"


# 与 tools._LOGISTICS_KEYWORDS 对齐：用户/槽位表明在问物流或提供了运单号
_LOGISTICS_CONTEXT_KW = (
    "快递",
    "物流",
    "运单",
    "包裹",
    "到哪",
    "发货",
    "几天",
    "配送",
    "签收",
)


def _has_tracking_or_logistics_context(
    user_input: str,
    intent_slots: dict[str, Any] | None,
    accumulated_slots: dict[str, Any] | None,
) -> bool:
    slots = intent_slots or {}
    if slots.get("tracking_no"):
        return True
    acc = accumulated_slots or {}
    if acc.get("tracking_nos"):
        return True
    u = user_input or ""
    if re.search(r"\d{8,}", u):
        return True
    return any(kw in u for kw in _LOGISTICS_CONTEXT_KW)


# 客户问「一般多久到」但未点名本人订单/单号时，禁止回复里再索要订单号、帮查物流
_SPECIFIC_ORDER_OR_TRACKING_USER_RE = re.compile(
    r"(?:我的|这笔|该笔|那个)?订单|订单号|运单|快递单号|"
    r"查(?:一下)?(?:我)?的?(?:物流|包裹)|物流详情|(?:包裹|货)(?:到)?哪",
)
_GENERAL_DELIVERY_ETA_USER_RE = re.compile(
    r"(?:大概|大约|一般)?\s*(?:多久|几天|什么时间|啥时候|什么时候)(?:能)?到|"
    r"^(?:多久|几天)(?:能)?到|"
    r"(?:收货|到货|拿到)(?:大概|大约|一般)?(?:多久|几天|什么时候)",
)


def _user_asks_general_delivery_eta_only(user_input: str) -> bool:
    u = (user_input or "").strip()
    if not u:
        return False
    if _SPECIFIC_ORDER_OR_TRACKING_USER_RE.search(u):
        return False
    return bool(_GENERAL_DELIVERY_ETA_USER_RE.search(u))


def _scrub_general_eta_order_pester(reply_parts: list[str], user_input: str) -> list[str]:
    """剔除「一般到货时效」问法下多余索要订单号、帮查物流的尾巴（模型常误拼）。"""
    if not _user_asks_general_delivery_eta_only(user_input):
        return reply_parts
    markers = (
        "您方便提供",
        "麻烦您提供",
        "麻烦提供",
        "能不能麻烦您",
        "方便提供一下订单号",
        "我帮您查",
        "帮您查下",
        "查一下最新",
        "最新的物流",
        "物流进度",
        "提供一下订单号",
    )
    out: list[str] = []
    for part in reply_parts:
        p = (part or "").strip()
        if not p:
            continue
        cut_at = len(p)
        for m in markers:
            i = p.find(m)
            if i >= 6 and i < cut_at:
                cut_at = i
        if cut_at < len(p):
            p = p[:cut_at].strip()
        p = re.sub(r"\s*[,，]+\s*$", "", p)
        if p:
            out.append(p)
    return out if out else reply_parts


def _resolve_tracking_display(
    user_input: str,
    intent_slots: dict[str, Any] | None,
    accumulated_slots: dict[str, Any] | None,
    reply_hint: str | None = None,
) -> str:
    slots = intent_slots or {}
    if slots.get("tracking_no"):
        return str(slots["tracking_no"])
    acc = accumulated_slots or {}
    nos = acc.get("tracking_nos") or []
    if nos:
        return str(nos[0])
    for src in (user_input, reply_hint):
        if not src:
            continue
        m = re.search(r"\d{8,}", src)
        if m:
            return m.group(0)
    return "该"


def _scrub_unverified_logistics_claims(
    reply_parts: list[str],
    user_input: str,
    order_result: Any,
    intent: str | None,
    intents: list[str] | None,
    intent_slots: dict[str, Any] | None,
    accumulated_slots: dict[str, Any] | None,
) -> list[str]:
    intents_list = list(intents) if intents else ([intent] if intent else [])
    if "order" not in intents_list:
        return reply_parts
    text = "\n".join((p or "").strip() for p in reply_parts if (p or "").strip())
    unverified = _order_logistics_is_unverified(order_result)
    if not unverified and isinstance(order_result, dict) and order_result.get("logistics") is None:
        # 订单 JSON 成功但本轮未写入 logistics（或并行合并异常）：有运单/物流语境时仍禁止编造轨迹
        unverified = _has_tracking_or_logistics_context(
            user_input, intent_slots, accumulated_slots
        ) or bool(re.search(r"\d{8,}", text))
    if not unverified:
        return reply_parts
    if not text or not _FANTASY_LOGISTICS_STATUS_RE.search(text):
        return reply_parts
    tn = _resolve_tracking_display(
        user_input, intent_slots, accumulated_slots, reply_hint=text
    )
    safe = random.choice(_UNVERIFIED_LOGISTICS_SAFE_TEMPLATES).format(tn=tn)
    logger.warning("[generator] 已拦截未验证物流轨迹下的在途/时效幻觉回复")
    return [safe]


def _hard_cut_text(text: str, max_len: int) -> list[str]:
    """兜底硬切分：当句子本身过长时按长度切块。"""
    t = (text or "").strip()
    if not t:
        return []
    return [t[i : i + max_len].strip() for i in range(0, len(t), max_len) if t[i : i + max_len].strip()]


def _split_by_discourse_markers(text: str) -> list[str]:
    """
    无明显句末标点时，按语义连接词切分为更自然的短句。
    仅在 marker 前切分，保留 marker 本身在后段开头。
    """
    t = (text or "").strip()
    if not t:
        return []
    markers = ("关于", "另外", "此外", "同时", "至于", "然后", "最后")
    idxs: list[int] = []
    for m in markers:
        start = 0
        while True:
            i = t.find(m, start)
            if i <= 0:
                break
            # 避免切得过碎：前后至少有一定内容
            if i >= 16 and (len(t) - i) >= 10:
                idxs.append(i)
            start = i + len(m)
    if not idxs:
        return [t]
    idxs = sorted(set(idxs))
    parts: list[str] = []
    prev = 0
    for i in idxs:
        seg = t[prev:i].strip()
        if seg:
            parts.append(seg)
        prev = i
    last = t[prev:].strip()
    if last:
        parts.append(last)
    return parts or [t]


def _inject_discourse_boundaries(text: str) -> str:
    """
    在"无标点直接转话题"的位置补可切分边界，便于后续按句切分。
    示例："...留香时间挺长的关于儿童使用..." -> "...留香时间挺长的。关于儿童使用..."
    """
    t = (text or "").strip()
    if not t:
        return t
    # 前一位不是常见断句/停顿标点或空白时，在话题连接词前补句号边界
    return re.sub(
        r"(?<![。！？!?；;，,、\s])(关于|另外|此外|同时|至于|然后|最后)",
        r"。\1",
        t,
    )


def _split_by_spaces_if_needed(text: str, max_len: int) -> list[str]:
    """
    针对"长段+空格串联"的口语输出做兜底切分。
    - 先按空格分词块
    - 再按 max_len 组装
    """
    t = (text or "").strip()
    if not t:
        return []
    if " " not in t:
        return [t]
    tokens = [x for x in re.split(r"\s+", t) if x]
    if not tokens:
        return [t]
    out: list[str] = []
    cur = ""
    for tok in tokens:
        if not cur:
            cur = tok
            continue
        candidate = f"{cur} {tok}"
        if len(candidate) <= max_len:
            cur = candidate
        else:
            out.append(cur.strip())
            cur = tok
    if cur:
        out.append(cur.strip())
    return out or [t]


def _strip_customer_markdown(text: str) -> str:
    """去掉模型仿写的 Markdown（**加粗**、`代码` 等）；商品价格上下文本身不含这些符号。"""
    if not text:
        return text
    s = text
    for _ in range(8):
        nxt = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.DOTALL)
        if nxt == s:
            break
        s = nxt
    for _ in range(8):
        nxt = re.sub(r"__(.+?)__", r"\1", s, flags=re.DOTALL)
        if nxt == s:
            break
        s = nxt
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = s.replace("**", "").replace("__", "")
    return s


def _strip_markdown_from_reply_parts(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        t = _cleanup_split_piece(_strip_customer_markdown(p or ""))
        if t:
            out.append(t)
    return out if out else [REPLY_SHORT_ACK]


def _cleanup_split_piece(text: str) -> str:
    """分条后统一清洗：去前导标点、压缩空白。"""
    t = (text or "").strip()
    t = t.lstrip("。；;，,、 ").strip()
    t = re.sub(r"\s+", " ", t)
    return t


# 日常对话句末边界识别：
# 1. 句末标点（。！？!?）后的空白
# 2. 任意空格/全角空格（qwen3.5-flash 常用单空格粘合多句，中文里正常不该出现）
# 3. 「，」后若跟着 ≥6 字的新句也算边界（模型偶尔用逗号连续粘多句）
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?])\s*| +|　+")
# 单条过短（<10 字）通常是承接词，不算完整一句，需与下一段合并
_MIN_BUBBLE_LEN = 10
# 单条超过此值一定要强制按句切（模型无视 [SPLIT] 的兜底保护）
_MAX_BUBBLE_LEN_BEFORE_FORCE = 55


def _auto_split_chat_bubble(text: str, max_bubbles: int = 5) -> list[str]:
    """
    日常对话自动分条：把一整段拆成 2~5 条气泡，模拟 IM 一条一条发的节奏。

    规则：
    - 含小程序链接的段不动
    - 按句末标点（。！？!?）/ 多空格切分
    - 过短片段（<10 字）与下一段合并，避免「好的，」单独一条
    - 返回 ≤ max_bubbles 条；若只切出 1 段说明本来就短，原样返回
    """
    t = (text or "").strip()
    if not t:
        return []
    if _MINIAPP_LINK_RE.search(t):
        return [t]
    # 模型常见输出：句末没写标点，用空格粘合多句（"可以的哦，干皮也能用 它虽然... 建议... 您是..."）
    raw = [s.strip() for s in _SENTENCE_BOUNDARY_RE.split(t) if s and s.strip()]
    if len(raw) <= 1:
        return [t]

    # 短片段向后合并
    merged: list[str] = []
    buf = ""
    for seg in raw:
        seg = _cleanup_split_piece(seg)
        if not seg:
            continue
        if buf and len(buf) < _MIN_BUBBLE_LEN:
            # buf 末尾若是中文/数字/字母（被空格切开的同一句），直接续接，避免"26，块"这种硬插逗号
            sep = "，" if buf[-1] in "。！？!?；;，,、" else ""
            buf = (buf + sep + seg).strip("，")
        elif buf:
            merged.append(buf)
            buf = seg
        else:
            buf = seg
    if buf:
        merged.append(buf)

    if len(merged) <= 1:
        return [t]
    if len(merged) > max_bubbles:
        # 超过上限：把末尾多出来的合并成最后一段
        tail = "，".join(merged[max_bubbles - 1:])
        merged = merged[: max_bubbles - 1] + [tail]
    return merged


def _apply_auto_chat_split(
    reply_parts: list[str],
    intent: str | None,
) -> list[str]:
    """
    日常对话气泡化：当模型无视 [SPLIT] 规则一整段输出时，按句拆成多条气泡。

    触发条件：
    - intent 为 knowledge / chitchat / unknown（日常对话类）
    - 整体只有 1 段且该段>=_MAX_BUBBLE_LEN_BEFORE_FORCE 字（短回复不动）
    - 不含小程序链接（链接本身独立成段已由其他逻辑保障）
    """
    if intent and intent not in ("knowledge", "chitchat", "unknown"):
        return reply_parts
    if len(reply_parts) != 1:
        return reply_parts
    only = (reply_parts[0] or "").strip()
    if not only or len(only) < _MAX_BUBBLE_LEN_BEFORE_FORCE:
        return reply_parts
    if _MINIAPP_LINK_RE.search(only):
        return reply_parts
    return _auto_split_chat_bubble(only)


def _split_long_reply_parts(reply_parts: list[str], max_len: int = 110) -> list[str]:
    """
    超长气泡二次分条：
    - 含小程序链接的气泡不切分
    - 优先按句末标点（。！？；）切，再按逗号切，最后硬切
    """
    out: list[str] = []
    for part in reply_parts:
        text = (part or "").strip()
        if not text:
            continue
        if _MINIAPP_LINK_RE.search(text):
            out.append(text)
            continue

        # 先注入语义边界，处理"无标点直接切话题"的长句粘连
        text = _inject_discourse_boundaries(text)

        # 即便未超长，只要出现明显话题切换，也尝试拆分成更易读的多条
        marker_split = _split_by_discourse_markers(text)
        if len(marker_split) > 1:
            # 对 marker 结果再做长度控制
            text = "\n".join(marker_split)

        if len(text) <= max_len and "\n" not in text:
            cleaned = _cleanup_split_piece(text)
            if cleaned:
                out.append(cleaned)
            continue

        # 若上面按 marker 切出了多段，优先按换行段进入后续流程
        pre_segments = [s.strip() for s in text.split("\n") if s.strip()]
        if not pre_segments:
            pre_segments = [text]

        chunks: list[str] = []
        # 每个 pre_segment 独立组包，避免跨"话题边界"回拼
        for seg0 in pre_segments:
            seg_sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;])\s*", seg0) if s.strip()]
            if not seg_sents:
                seg_sents = [seg0]
            if len(seg_sents) == 1 and len(seg_sents[0]) > max_len:
                seg_sents = _split_by_discourse_markers(seg_sents[0])

            current = ""
            for sent in seg_sents:
                if len(sent) > max_len:
                    segs = [x.strip() for x in re.split(r"(?<=[，,、])\s*", sent) if x.strip()]
                    if not segs:
                        segs = [sent]
                else:
                    segs = [sent]
                if len(segs) == 1 and len(segs[0]) > max_len and " " in segs[0]:
                    segs = _split_by_spaces_if_needed(segs[0], max_len)

                for seg in segs:
                    if len(seg) > max_len:
                        if current:
                            chunks.append(current.strip())
                            current = ""
                        chunks.extend(_hard_cut_text(seg, max_len))
                        continue
                    if not current:
                        current = seg
                        continue
                    if len(current) + len(seg) <= max_len:
                        current += seg
                    else:
                        chunks.append(current.strip())
                        current = seg
            if current:
                chunks.append(current.strip())
        cleaned_chunks = [
            _cleanup_split_piece(c) for c in (chunks if chunks else [text])
            if _cleanup_split_piece(c)
        ]
        out.extend(cleaned_chunks if cleaned_chunks else [text])
    return out if out else [REPLY_SHORT_ACK]


def _force_links_as_standalone_parts(reply_parts: list[str]) -> list[str]:
    """
    最终硬保障：无论前面经过多少清洗/切分，只要出现小程序链接，都强制拆成独立一条。
    这样 CRM 可稳定识别并转卡片。
    """
    out: list[str] = []
    for part in reply_parts:
        text = (part or "").strip()
        if not text:
            continue
        links = _MINIAPP_LINK_RE.findall(text)
        if not links:
            cleaned = _cleanup_split_piece(text)
            if cleaned:
                out.append(cleaned)
            continue
        body = _MINIAPP_LINK_RE.sub("", text).strip(" \n\t，,。.;；")
        body = _cleanup_split_piece(body) if body else ""
        if body:
            out.append(body)
        for link in links:
            out.append(link.strip())
    return out if out else [REPLY_SHORT_ACK]


def _scrub_unauthorized_handoff_promises(
    reply_parts: list[str],
    user_input: str,
    need_human_transfer: bool,
) -> list[str]:
    """
    当系统未判定转人工时，禁止输出"马上转人工/请稍等1~3分钟"等承诺。
    对于用户软性"转人工"请求，先澄清需求，不直接承诺已转接。
    """
    if need_human_transfer:
        return reply_parts
    if not any(_HANDOFF_PROMISE_RE.search(p or "") for p in reply_parts):
        return reply_parts
    out: list[str] = []
    for p in reply_parts:
        t = _cleanup_split_piece(_HANDOFF_PROMISE_RE.sub("", p or ""))
        if t:
            out.append(t)
    if out:
        return out
    if _SOFT_HANDOFF_USER_RE.search(user_input or ""):
        return [_SOFT_HANDOFF_CLARIFY_REPLY]
    return [REPLY_SHORT_ACK]


def _enforce_special_population_policy(reply_parts: list[str], user_input: str) -> list[str]:
    """
    儿童/孕妇问题的口径硬约束：
    - 禁止出现"没有专项结论/仅建议咨询医生"的旧说法
    - 统一落地为：6岁以上可用 + 特殊人群不建议使用（必要时再补咨询医生）
    """
    if not _SPECIAL_POP_QUESTION_RE.search(user_input or ""):
        return reply_parts
    text = "\n".join((p or "").strip() for p in reply_parts if (p or "").strip())
    has_outdated = bool(_OUTDATED_SPECIAL_POP_RE.search(text))
    if not has_outdated:
        return reply_parts

    # 先尽量保留当前"6岁以上可用"信息，再补强最新特殊人群口径
    kept: list[str] = []
    for p in reply_parts:
        t = (p or "").strip()
        if not t:
            continue
        t = _OUTDATED_SPECIAL_POP_RE.sub("", t).strip(" ，,。.;；")
        if t:
            kept.append(t)
    merged = "\n".join(kept)
    if "6岁以上" not in merged:
        kept.insert(0, "这款资料里明确写的是适用于6岁以上人群，男女通用。")
    kept.append(_SPECIAL_POP_POLICY)
    return kept


def _finalize_reply_parts(
    reply_parts: list[str],
    user_input: str,
    history_msgs: list,
    scene: str | None,
    field_updates: dict[str, Any] | None,
    intent: str | None,
    need_human_transfer: bool = False,
    wants_purchase_link: bool | None = None,
    brand: str = "",
    role: str = "专业客服",
    purchase_link: str = "",
) -> tuple[list[str], str]:
    """统一：频控 normalize → 试用后/显式购买补链 → 剔除空头承诺 → 拼接 full_reply。

    wants_purchase_link：来自 analysis_node 的 LLM 语义判断（可选）。优先于正则。
    """
    # 单独"1"语义歧义大：优先澄清客户目的，避免误发链接或只回"收到"。
    if _whole_message_is_lone_one(user_input):
        clarify = REPLY_CHITCHAT_INTENT_CLARIFY.format(brand=brand, role=role)
        return [clarify], clarify

    allow_links = _allow_link_this_turn(user_input, history_msgs, wants_purchase_link)
    parts = _normalize_reply_parts(list(reply_parts), allow_links=allow_links)
    # 日常对话兜底：模型无视 [SPLIT] 直接整段输出时，按句强制拆成气泡
    parts = _apply_auto_chat_split(parts, intent)
    if allow_links and _should_append_followup_link(
        scene, field_updates, intent, user_input, parts, history_msgs
    ):
        parts.append(purchase_link)
        logger.info("[generator] 触达后正向反馈命中，自动补发购买链接 scene=%s", scene)
    elif _should_append_purchase_link_fallback(intent, user_input, parts, allow_links, wants_purchase_link):
        parts.append(purchase_link)
        logger.info("[generator] 明确购买信号命中，自动补发购买链接")
    parts = _scrub_hollow_link_promises(parts)
    parts = _scrub_session_link_quota_copy(parts, user_input)
    parts = _scrub_unauthorized_handoff_promises(parts, user_input, need_human_transfer)
    parts = _enforce_special_population_policy(parts, user_input)
    parts = _maybe_clarify_weak_chitchat_reply(parts, intent, user_input, brand=brand, role=role)
    parts = _upgrade_purchase_ack_when_linked(parts, user_input)
    parts = _ensure_explicit_purchase_reply_substance(parts, user_input, allow_links, purchase_link)
    parts = _scrub_general_eta_order_pester(parts, user_input)
    parts = _split_long_reply_parts(parts, max_len=110)
    parts = _force_links_as_standalone_parts(parts)
    parts = _strip_markdown_from_reply_parts(parts)
    return parts, "\n".join(parts)


def _extract_scene_meta(reply: str) -> tuple[int | None, str | None, str]:
    """
    从 LLM 回复中提取场景化结构化标签并剥离。

    Returns:
        (intent_level_delta, next_stage_suggestion, cleaned_reply)
        若未匹配到标签，前两项为 None，cleaned_reply 即原文（仅做 strip）。
    """
    match = _SCENE_META_RE.search(reply)
    if not match:
        # 兜底：若模型输出了变体标签，仍强制剥离，避免回传给客户
        cleaned = _SCENE_META_FUZZY_RE.sub("", reply).rstrip(" \n\t,，；;。.")
        return None, None, cleaned

    try:
        delta = int(match.group("delta"))
        # clamp 到 [-30, 30]，防止模型给出离谱值
        delta = max(-30, min(30, delta))
    except ValueError:
        delta = None
    stage = match.group("stage").lower()

    cleaned = _SCENE_META_RE.sub("", reply).rstrip(" \n\t,，；;。.")
    return delta, stage, cleaned


def _has_link_in_history(messages: list) -> bool:
    for msg in messages:
        if isinstance(msg, AIMessage) and isinstance(msg.content, str) and _MINIAPP_LINK_RE.search(msg.content):
            return True
    return False


def _allow_link_this_turn(user_input: str, history_msgs: list, wants_purchase_link: bool | None = None) -> bool:
    """
    链接频控：
    - 本会话尚未发过链接：允许首次发送
    - 已发过链接：仅当用户明确表达购买意向时允许再次发送

    wants_purchase_link 由 analysis_node 的 LLM 语义判断输出，优先于正则。
    """
    if not _has_link_in_history(history_msgs):
        return True
    # 显式购买信号统一复用 _user_explicit_wants_link：
    # 其中已包含"整句仅 1（含前后空白）"与"扣1/回1/回复1/打1"等口令。
    return _user_explicit_wants_link(user_input or "", wants_purchase_link)


def _trial_feedback_neutral_or_above(
    user_input: str,
    trial_used_just_detected: bool,
    in_followup_scene: bool,
) -> bool:
    """
    体验后中性及以上评价：
    - 显式「用了+好评」紧挨；或
    - 文本含中性/好评词、无强负向、无体验轻量抱怨，且（本轮刚判 trial_used / 或已在 trial_used·首单关怀场景）
    """
    if _POST_TRIAL_POSITIVE_RE.search(user_input or ""):
        return True
    if _TRIAL_EVAL_STRONG_NEGATIVE_RE.search(user_input or ""):
        return False
    # 轻量体验抱怨（干/紧绷/味道怪/黏腻等）：本轮应先答体验问题，不自动补链
    if _TRIAL_EVAL_MILD_COMPLAINT_RE.search(user_input or ""):
        return False
    if not _TRIAL_EVAL_NEUTRAL_OR_POSITIVE_RE.search(user_input or ""):
        return False
    return trial_used_just_detected or in_followup_scene


def _should_append_followup_link(
    scene: str | None,
    field_updates: dict[str, Any] | None,
    intent: str | None,
    user_input: str,
    reply_parts: list[str],
    history_msgs: list,
) -> bool:
    """
    触达后跟进兜底补链路：
    - 未体验客户跟进：analysis 本轮判 trial_used 且客户评价中性及以上 → 推一次链接
    - 或已在 trial_used / first_order_no_rebuy 且同类反馈
    - 本轮模型未产出链接；会话历史未发过链接
    """
    # 两类场景允许触发：
    # 1) 已在试用后/首单未复购场景
    # 2) 当前仍是 trial_not_used，但 analysis 本轮已识别到 trial_used=True（首个关键反馈轮）
    trial_used_just_detected = bool((field_updates or {}).get("trial_used"))
    in_followup_scene = scene in {"trial_used", "first_order_no_rebuy"}
    if not (in_followup_scene or trial_used_just_detected):
        return False
    if _has_link_in_history(history_msgs):
        return False
    if intent and intent != "knowledge":
        return False
    if _POST_ORDER_CONTEXT_RE.search(user_input or ""):
        return False
    if not _trial_feedback_neutral_or_above(
        user_input or "", trial_used_just_detected, in_followup_scene
    ):
        return False
    if any(_MINIAPP_LINK_RE.search(p or "") for p in reply_parts):
        return False
    return True


def _should_append_purchase_link_fallback(
    intent: str | None,
    user_input: str,
    reply_parts: list[str],
    allow_links: bool,
    wants_purchase_link: bool | None = None,
) -> bool:
    """
    显式购买信号兜底补链路：
    - 用户本轮明确在问购买/链接（与 intent 无关：避免 chitchat/unknown 误分导致漏补链）
    - 频控允许发链接
    - 本轮回复尚未包含小程序链接

    wants_purchase_link 由 analysis_node 的 LLM 语义判断输出，优先于正则。
    """
    _ = intent
    if not allow_links:
        return False
    if _POST_ORDER_CONTEXT_RE.search(user_input or ""):
        return False
    if not _user_explicit_wants_link(user_input, wants_purchase_link):
        return False
    if any(_MINIAPP_LINK_RE.search(p or "") for p in reply_parts):
        return False
    return True


def _normalize_reply_parts(parts: list[str], allow_links: bool = True) -> list[str]:
    """
    规范 reply_parts：
    - 清理空白
    - 若包含小程序链接，强制将链接拆成单独一条消息
    """
    normalized: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        links = _MINIAPP_LINK_RE.findall(text)
        if not allow_links and links:
            # 历史已发过且本轮非明确购买意向：移除链接，仅保留正文
            text = _MINIAPP_LINK_RE.sub("", text).strip(" \n\t，,。.;；")
            if text:
                normalized.append(text)
            continue
        if not links:
            normalized.append(text)
            continue
        # 移除链接后正文单独一条，链接逐条单独发送
        body = _MINIAPP_LINK_RE.sub("", text).strip(" \n\t，,。.;；")
        if body:
            normalized.append(body)
        for link in links:
            normalized.append(link)
    return normalized or [""]


@lru_cache(maxsize=1)
def _build_llm():
    """创建生成模型实例，模型名从 settings.generator_model 读取。"""
    import httpx as _httpx
    from langchain_openai import ChatOpenAI
    from agent_framework.config.settings import get_settings
    s = get_settings()
    extra_body = {}
    extra_body = {"enable_thinking": False}  # 关闭 thinking 防止 reasoning tokens 吃光 max_tokens
    async_client = _httpx.AsyncClient(
        limits=_httpx.Limits(max_connections=200, max_keepalive_connections=80),
        timeout=30.0,
    )
    return ChatOpenAI(
        model=s.generator_model,
        api_key=s.effective_api_key,
        base_url=s.openai_base_url,
        temperature=s.generator_temperature,
        max_tokens=s.openai_max_tokens,
        http_async_client=async_client,
        **({"extra_body": extra_body} if extra_body else {}),
    )




async def _summarize_history(messages: list, existing_summary: str) -> str:
    """
    将超出窗口的历史消息压缩为摘要。
    若已有摘要，则在其基础上追加新内容。
    """
    lines = []
    if existing_summary:
        lines.append(f"[已有摘要]\n{existing_summary}\n")
    lines.append("[需要追加摘要的对话片段]")
    for msg in messages:
        role = "用户" if isinstance(msg, HumanMessage) else "客服"
        lines.append(f"{role}：{msg.content}")

    prompt = (
        "你是电商客服对话摘要助手。请将以下电商客服对话内容压缩为简洁摘要，"
        "保留关键信息（用户问题、意图、已解决事项、"
        "未解决问题、重要槽位如订单号/商品名等），摘要控制在150字以内，使用中文。\n\n"
        + "\n".join(lines)
    )
    try:
        llm = _build_llm()
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return resp.content.strip()
    except Exception as e:
        err_str = str(e)
        if "data_inspection_failed" in err_str:
            logger.warning("[generator] 摘要内容触发内容安全，跳过本次摘要")
            return ""
        logger.warning("[generator] 摘要生成失败，保留原摘要: %s", e)
        return existing_summary


async def _compress_phases_to_master(phase_summaries: list[str], existing_master: str) -> str:
    """
    将多个阶段摘要压缩为总摘要（300 字上限）。
    若已有 master，将其作为背景一并压缩。
    """
    lines = []
    if existing_master:
        lines.append(f"[已有总摘要]\n{existing_master}\n")
    lines.append("[各阶段摘要]")
    for i, phase in enumerate(phase_summaries, 1):
        lines.append(f"阶段{i}：{phase}")

    prompt = (
        "你是电商客服对话摘要助手。请将以下多个电商客服对话阶段摘要压缩为一份总摘要，"
        "保留关键信息（用户意图变化、"
        "已解决事项、未解决问题、重要槽位如订单号/商品名等），总摘要控制在300字以内，使用中文。\n\n"
        + "\n".join(lines)
    )
    try:
        llm = _build_llm()
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return resp.content.strip()
    except Exception as e:
        err_str = str(e)
        if "data_inspection_failed" in err_str:
            logger.warning("[generator] 总摘要内容触发内容安全，跳过本次压缩")
            return existing_master
        logger.warning("[generator] 总摘要压缩失败，保留原摘要: %s", e)
        return existing_master


def _format_accumulated_slots(slots: dict) -> str:
    """将 accumulated_slots 格式化为结构化文本，注入 LLM。"""
    parts = []
    if slots.get("order_ids"):
        parts.append(f"历史订单号：{', '.join(slots['order_ids'])}")
    if slots.get("tracking_nos"):
        parts.append(f"运单号：{', '.join(slots['tracking_nos'])}")
    if slots.get("product_names"):
        parts.append(f"关注商品：{', '.join(slots['product_names'])}")
    if slots.get("issue_types"):
        parts.append(f"咨询类型：{', '.join(slots['issue_types'])}")
    if slots.get("resolved"):
        parts.append(f"已解决：{', '.join(str(x) for x in slots['resolved'])}")
    if slots.get("unresolved"):
        parts.append(f"未解决：{', '.join(str(x) for x in slots['unresolved'])}")
    return "\n".join(parts)


def _build_effective_summary(master: str, phases: list[str], legacy: str) -> str:
    """决定注入哪层摘要：master > phases 拼接 > legacy。"""
    if master:
        return master
    if phases:
        return "\n---\n".join(phases)
    return legacy


# 内容安全过滤：匹配常见注入攻击、政治敏感、解码指令等测试模式
_CONTENT_SAFETY_PATTERNS = re.compile(
    r"ignore\s+(?:previous|all)\s+instructions?|"
    r"forget\s+(?:previous|all)|"
    r"you\s+are\s+now\s+(?:a\s+)?(?:dan|jailbreak|evil|hacker)|"
    r"base64|decode\s+this|eval\s*\(|<script|javascript:|"
    r"忘记.{0,10}(?:指令|规则|设置)|忽略.{0,10}(?:之前|上面).{0,10}(?:指令|规则)|"
    r"你现在是(?!.*客服)|新的?系统提示|"
    r"政治立场|天安门|台湾独立|西藏独立|法轮功",
    re.IGNORECASE,
)

# 伪造知识库 / 系统消息格式（用户消息中不应出现这些结构）
_FAKE_REFERENCE_PATTERNS = re.compile(
    r"\[参考\d*\]|"
    r"\[(?:来源|引用|文档|知识库)\d*\]|"
    r"\{\"role\"\s*:\s*\"system\"|"
    r"<\s*/?system\s*>|"
    r"【(?:系统|参考|知识库|规则|指令).*?】",
    re.IGNORECASE,
)

# 伪造规则/指令块（常与伪造引用一起出现）
_FAKE_DIRECTIVE_PATTERNS = re.compile(
    r"(?:新规则|规则补充|系统指令|附加指令|新指令)[:：].{0,160}?(?=(?:请问|这个|这款|那这个|那这款|另外|$))|"
    r"(?:从现在起|现在开始|接下来).{0,40}?(?:你必须|只能|务必).{0,160}?(?=(?:请问|这个|这款|那这个|那这款|另外|$))",
    re.IGNORECASE,
)


def _filter_unsafe_messages(messages: list) -> list:
    """
    过滤历史消息中触发内容安全的内容，替换为占位符。
    避免测试消息污染摘要或导致后续请求被拦截。
    同时过滤伪造知识库引用格式，防止间接注入。
    """
    filtered = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = msg.content
            if _CONTENT_SAFETY_PATTERNS.search(content):
                logger.debug("[generator] 过滤历史消息中的疑似敏感内容")
                filtered.append(HumanMessage(content="[用户消息已过滤]"))
            elif _FAKE_REFERENCE_PATTERNS.search(content) or _FAKE_DIRECTIVE_PATTERNS.search(content):
                logger.debug("[generator] 过滤历史消息中的伪造引用/规则格式")
                cleaned = _FAKE_REFERENCE_PATTERNS.sub("", content)
                cleaned = _FAKE_DIRECTIVE_PATTERNS.sub("", cleaned).strip()
                filtered.append(HumanMessage(content=cleaned if cleaned else "[用户消息已过滤]"))
            else:
                filtered.append(msg)
        else:
            filtered.append(msg)
    return filtered


def _build_history_messages(
    messages: list,
    summary: str,
) -> list:
    """
    构建传给 LLM 的历史消息列表（摘要缓冲混合记忆）：
    - 历史超出 K_MESSAGES 条时，旧部分压缩为摘要注入 system
    - 最近 K_MESSAGES 条直接传递
    返回 (history_msgs, needs_summarize, msgs_to_summarize)
    """
    # 过滤掉非 Human/AI 的消息（如 ToolMessage）
    dialog = [m for m in messages if isinstance(m, (HumanMessage, AIMessage))]

    if len(dialog) <= K_MESSAGES:
        return dialog, False, []

    old_msgs = dialog[:-K_MESSAGES]
    recent_msgs = dialog[-K_MESSAGES:]
    return recent_msgs, True, old_msgs


async def generator_node(state: CustomerServiceState) -> dict[str, Any]:
    """
    回复生成节点（异步）。

    记忆策略（分级摘要 + 槽位）：
    - 最近 K_RECENT 轮（6轮）直接传给 LLM
    - 每超出 K_MESSAGES 条触发一次阶段摘要（独立压缩）
    - 每 5 个阶段摘要触发一次 master 压缩，phase 列表清空
    - accumulated_slots 跨轮注入已知信息
    - 24h 无活动超时重置摘要

    输入:  system_prompt, context_text, user_input, session_id, messages,
           conversation_summary, phase_summaries, master_summary, last_active_time, accumulated_slots
    输出:  final_reply, reply_tokens, messages, conversation_summary,
           phase_summaries, master_summary, last_active_time
    """
    TIMEOUT_SECONDS = 24 * 3600

    t0 = time.time()
    system_prompt    = state.get("system_prompt", "")
    context_text     = state.get("context_text", "")
    user_input       = state.get("user_input", "")
    session_id       = state.get("session_id", "")
    existing_msgs    = list(state.get("messages") or [])
    existing_summary = state.get("conversation_summary") or ""
    phase_summaries  = list(state.get("phase_summaries") or [])
    master_summary   = state.get("master_summary") or ""
    last_active_time = state.get("last_active_time")
    accumulated_slots = dict(state.get("accumulated_slots") or {})
    full_system      = f"{system_prompt}\n\n{context_text}".strip()

    # ── 24h 超时重置 ─────────────────────────────────────
    # last_active_time 为 None 说明 checkpoint 已过期重建（Redis TTL 触发），
    # 此时 phase_summaries / master_summary 也已随 checkpoint 清空，无需重置；
    # 但若 checkpoint 意外保留了旧摘要（TTL 延长等情况），仍需兜底重置。
    _now = time.time()
    _timed_out = last_active_time is None or (_now - last_active_time) > TIMEOUT_SECONDS
    if _timed_out and (phase_summaries or master_summary or existing_summary):
        logger.info("[generator] session=%s %s，重置摘要",
                    session_id, "checkpoint 重建" if last_active_time is None else "超过24h未活动")
        phase_summaries  = []
        master_summary   = ""
        existing_summary = ""
        # 保留历史关键槽位（订单号、商品名等），合并本轮新槽位
        from agents.customer_service.nodes.analysis import _merge_slots
        # 只保留 order_ids / product_names，丢弃 issue_types / resolved 等过期状态
        retained = {
            "order_ids":     accumulated_slots.get("order_ids", []),
            "tracking_nos":  accumulated_slots.get("tracking_nos", []),
            "product_names": accumulated_slots.get("product_names", []),
        }
        accumulated_slots = _merge_slots(retained, state.get("intent_slots") or {}, state.get("intent", ""))

    # ── 加载历史：优先 ES，降级为 state.messages ──────────
    # ES 只取最近 40 条（20轮），更早的历史由 PG 的 phase_summaries/master_summary 覆盖
    from agent_framework.memory.es_history import load_session_history, ES_INDEX
    if ES_INDEX and session_id:
        es_msgs = await load_session_history(session_id, max_messages=40)
        history_msgs = es_msgs if es_msgs else existing_msgs
        history_source = "es" if es_msgs else "state"
    else:
        history_msgs = existing_msgs
        history_source = "state"
    history_msgs = _filter_unsafe_messages(history_msgs)

    # ── 分级摘要缓冲（后台异步，不阻塞回复）──────────────
    recent_msgs, needs_summarize, old_msgs = _build_history_messages(
        history_msgs, existing_summary
    )
    summarize_task = None
    if needs_summarize:
        async def _do_summarize():
            nonlocal phase_summaries, master_summary
            new_phase = await _summarize_history(old_msgs, "")
            phase_summaries = phase_summaries + [new_phase]
            logger.info("[generator] 新增阶段摘要，共 %d 个（压缩 %d 条，来源=%s）",
                        len(phase_summaries), len(old_msgs), history_source)
            if len(phase_summaries) >= 5:
                master_summary = await _compress_phases_to_master(phase_summaries, master_summary)
                phase_summaries = []
                logger.info("[generator] 触发 master 压缩，phase 列表已清空")
        summarize_task = asyncio.create_task(_do_summarize())

    # ── Generator 缓存检查 ───────────────────────────────
    intent = state.get("intent", "unknown")
    has_dynamic_slots = any(
        (state.get("accumulated_slots") or {}).get(k)
        for k in ("order_ids", "tracking_nos")
    )
    cache_key = None
    # scene 模式下要求每次生成 <<DELTA,STAGE>>，跳过缓存避免污染
    is_scene_mode = bool(state.get("scene"))
    if intent in _CACHEABLE_INTENTS and not has_dynamic_slots and not is_scene_mode:
        cache_key = _generator_cache.make_key(intent, context_text, user_input, system_prompt)
        cached = _generator_cache.get(cache_key)
        if cached:
            _cached_reply, cached_parts = cached
            _intents_cached = state.get("intents")
            reply_parts = _scrub_unverified_logistics_claims(
                list(cached_parts),
                user_input,
                state.get("order_result"),
                state.get("intent"),
                _intents_cached if isinstance(_intents_cached, list) else None,
                state.get("intent_slots"),
                state.get("accumulated_slots"),
            )
            reply_parts, full_reply = _finalize_reply_parts(
                reply_parts,
                user_input,
                history_msgs,
                state.get("scene"),
                state.get("field_updates"),
                state.get("intent"),
                bool(state.get("need_human_transfer")),
                wants_purchase_link=state.get("wants_purchase_link"),
                brand=state.get("brand", ""),
                role=state.get("role", "专业客服"),
                purchase_link=_get_purchase_link(state),
            )
            new_messages = list(existing_msgs)
            new_messages.append(HumanMessage(content=user_input))
            new_messages.append(AIMessage(content=full_reply))
            if len(new_messages) > 40:
                new_messages = new_messages[-40:]
            # 等待后台摘要完成（如有）
            if summarize_task:
                try:
                    await asyncio.wait_for(summarize_task, timeout=5.0)
                except Exception:
                    pass
            debug_info = dict(state.get("debug_info") or {})
            debug_info["generator_ms"]     = round((time.time() - t0) * 1000)
            debug_info["generator_cached"] = True
            debug_info["reply_len"]        = len(full_reply)
            debug_info["reply_parts"]      = len(reply_parts)
            debug_info["history_rounds"]   = len(recent_msgs) // 2
            debug_info["history_source"]   = history_source
            debug_info["phase_count"]      = len(phase_summaries)
            debug_info["has_master"]       = bool(master_summary)
            logger.info("[generator] 缓存命中: intent=%s reply_len=%d elapsed=%dms",
                        intent, len(full_reply), debug_info["generator_ms"])
            return {
                "final_reply":          full_reply,
                "reply_parts":          reply_parts,
                "messages":             new_messages,
                "conversation_summary": existing_summary,
                "phase_summaries":      phase_summaries,
                "master_summary":       master_summary,
                "last_active_time":     time.time(),
                "debug_info":           debug_info,
            }

    # ── 组装 LLM 消息序列 ────────────────────────────────
    llm_messages: list = [SystemMessage(content=full_system)]
    if accumulated_slots and any(accumulated_slots.get(k) for k in ("order_ids", "product_names", "issue_types")):
        slot_text = _format_accumulated_slots(accumulated_slots)
        if slot_text:
            llm_messages.append(SystemMessage(content=f"[本次会话已知信息]\n{slot_text}"))
    effective_summary = _build_effective_summary(master_summary, phase_summaries, existing_summary)
    if effective_summary:
        llm_messages.append(SystemMessage(content=f"[对话历史摘要]\n{effective_summary}"))
    llm_messages.extend(recent_msgs)
    # 清洗当前用户输入中的伪造引用/规则格式，防止间接注入
    safe_user_input = _FAKE_REFERENCE_PATTERNS.sub("", user_input)
    safe_user_input = _FAKE_DIRECTIVE_PATTERNS.sub("", safe_user_input).strip()
    llm_messages.append(HumanMessage(content=safe_user_input if safe_user_input else "[用户消息已过滤]"))

    # ── 调用 LLM 生成回复（最多重试 2 次，含空回复重试）───
    reply = None
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            llm = _build_llm()
            resp = await llm.ainvoke(llm_messages)
            reply = resp.content
            if reply and len(reply.strip()) <= 15 and attempt < max_attempts - 1:
                logger.warning("[generator] 回复过短(%d字)，重试 attempt=%d", len(reply.strip()), attempt + 1)
                await asyncio.sleep(0.5)
                continue
            break
        except Exception as e:
            err_str = str(e).lower()
            is_retryable = any(code in err_str for code in ["429", "503", "rate limit", "overloaded", "timeout", "timed out"])
            if attempt < max_attempts - 1 and is_retryable:
                wait = 2 ** attempt  # 1s, 2s
                logger.warning("[generator] 第%d次调用失败，%.0fs后重试: %s", attempt + 1, wait, e)
                await asyncio.sleep(wait)
            else:
                logger.error("[generator] 生成失败（已重试%d次）: %s", attempt, e, exc_info=True)
                reply = "抱歉，系统暂时开小差了，请稍后再试，或联系人工客服为您服务。"
                break

    # 提取场景化结构化标签 <<DELTA:+10,STAGE:awaiting_reply_3>>（若有）
    intent_level_delta, next_stage_suggestion, reply = _extract_scene_meta(reply)

    # 按 [SPLIT] 拆分成多条消息（模拟人类客服分条发送）
    # 模型偶发输出小写 [split] 或带空格变体，统一归一化为 [SPLIT] 避免被当字面文本下发
    reply = re.sub(r"\[\s*split\s*\]", "[SPLIT]", reply, flags=re.IGNORECASE)
    reply_parts = [p.strip() for p in reply.split("[SPLIT]") if p.strip()]
    if not reply_parts:
        reply_parts = [reply]
    _intents_live = state.get("intents")
    reply_parts = _scrub_unverified_logistics_claims(
        reply_parts,
        safe_user_input,
        state.get("order_result"),
        state.get("intent"),
        _intents_live if isinstance(_intents_live, list) else None,
        state.get("intent_slots"),
        state.get("accumulated_slots"),
    )
    scene = state.get("scene")
    field_updates = state.get("field_updates")
    intent = state.get("intent")
    reply_parts, full_reply = _finalize_reply_parts(
        reply_parts, user_input, history_msgs, scene, field_updates, intent,
        bool(state.get("need_human_transfer")),
        wants_purchase_link=state.get("wants_purchase_link"),
        brand=state.get("brand", ""),
        role=state.get("role", "专业客服"),
        purchase_link=_get_purchase_link(state),
    )

    # 写入 generator 缓存（仅可缓存的意图）
    if cache_key:
        _generator_cache.put(cache_key, full_reply, reply_parts)

    # 等待后台摘要完成（如有）
    if summarize_task:
        try:
            await asyncio.wait_for(summarize_task, timeout=5.0)
        except Exception:
            pass

    # 更新 state.messages（只保留最近 40 条 = 20 轮，更早的已进摘要，无需重复存 PG）
    new_messages = list(existing_msgs)
    new_messages.append(HumanMessage(content=user_input))
    new_messages.append(AIMessage(content=full_reply))
    if len(new_messages) > 40:
        new_messages = new_messages[-40:]

    debug_info = dict(state.get("debug_info") or {})
    debug_info["generator_ms"]     = round((time.time() - t0) * 1000)
    debug_info["generator_cached"] = False
    debug_info["reply_len"]        = len(full_reply)
    debug_info["reply_parts"]      = len(reply_parts)
    debug_info["history_rounds"]   = len(recent_msgs) // 2
    debug_info["history_source"]   = history_source
    debug_info["phase_count"]      = len(phase_summaries)
    debug_info["has_master"]       = bool(master_summary)

    logger.info(
        "[generator] reply_len=%d parts=%d history=%d轮 source=%s phases=%d master=%s elapsed=%dms",
        len(full_reply), len(reply_parts), len(recent_msgs) // 2, history_source,
        len(phase_summaries), bool(master_summary), debug_info["generator_ms"],
    )
    return {
        "final_reply":          full_reply,
        "reply_parts":          reply_parts,
        "messages":             new_messages,
        "conversation_summary": existing_summary,
        "phase_summaries":      phase_summaries,
        "master_summary":       master_summary,
        "last_active_time":     time.time(),
        "intent_level_delta":   intent_level_delta,
        "next_stage_suggestion": next_stage_suggestion,
        "debug_info":           debug_info,
    }


