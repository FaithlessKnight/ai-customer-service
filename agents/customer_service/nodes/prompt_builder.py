"""
Prompt 构建节点

整合人物背景、工具结果、检索结果、对话历史，
构建最终发送给生成模型的完整系统提示词。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from agents.customer_service.state import CustomerServiceState
from agents.customer_service.prompts import (
    SYSTEM_PROMPT_BASE,
    CONTEXT_ORDER,
    CONTEXT_KNOWLEDGE, CONTEXT_KNOWLEDGE_GENERIC, CONTEXT_CHITCHAT,
    CONTEXT_TOOL_ERROR, CONTEXT_COMBINED, CONTEXT_HUMAN_TRANSFER,
    SCENE_LABELS, SCENE_SCRIPT_INSTRUCTION, OUTREACH_INSTRUCTION,
)

logger = logging.getLogger(__name__)

# awaiting_reply_N 自然语言提示：N 表示客户连续未回复次数（CRM 传入，1~5 封顶）
# trial_used：客户反馈试用感受（中性及以上），需要承接→活动价→链，避免堆砌卖点
_TRIAL_USE_FEEDBACK_RE = re.compile(
    r"(?:用(?:了|过|一下)|试(?:了|过)|洗(?:了|过)|还可以|还行|不错|挺好|满意|一般般|感觉)",
    re.IGNORECASE,
)


_AWAITING_HINTS = {
    1: "客户已1次未回复，本轮按【第1轮·提醒使用】话术，温和提醒不催促",
    2: "客户已连续2次未回复，本轮按【第2轮·活动告知】话术，可顺带说一下买赠活动",
    3: "客户已连续3次未回复，本轮按【第3轮·产品功能】话术，介绍核心特点引兴趣",
    4: "客户已连续4次未回复，本轮按【第4轮·性价比对比】话术，最后一次正向推进",
    5: "客户已连续5次未回复（封顶），本轮严格走【第5轮·低压退出】，不再追问/不再推产品/不发链接，礼貌收口",
}


def _format_journey_stage_hint(journey_stage: Any) -> str:
    """把 awaiting_reply_N / 整数枚举 1~5 翻译成自然语言阶段提示，其他 stage 原样展示。"""
    if journey_stage is None or isinstance(journey_stage, bool):
        return ""
    n: int | None = None
    if isinstance(journey_stage, int):
        if 1 <= journey_stage <= 5:
            n = journey_stage
    elif isinstance(journey_stage, str):
        s = journey_stage.strip()
        if not s:
            return ""
        if s.isdigit() and 1 <= int(s) <= 5:
            n = int(s)
        elif s.startswith("awaiting_reply_"):
            suffix = s.rsplit("_", 1)[-1]
            if suffix.isdigit():
                n = max(1, min(5, int(suffix)))
        else:
            return f"\n当前阶段：{s}"
    if n is None:
        return ""
    return f"\n当前阶段：awaiting_reply_{n}（{_AWAITING_HINTS[n]}）"


# ── 价格相关关键词（命中时查询商品缓存注入价格信息）───────────────
_PRICE_KEYWORDS = {
    "多少钱",
    "价格",
    "几块",
    "几元",
    "贵不贵",
    "便宜",
    "更便宜",
    "便宜款",
    "低价",
    "实惠",
    "性价比",
    "有没有别的",
    "还有别的",
    "别的款",
    "小瓶",
    "优惠",
    "打折",
    "活动",
    "促销",
    "怎么卖",
    "咋卖",
    "售价",
    "报价",
    "划算",
    "买一",
    "买二",
    "买三",
    "买四",
    "买五",
    "送一",
    "送二",
    "送三",
    "买赠",
    "套餐",
    "囤货",
    "到手价",
    "到手多少",
}


# 客户在问"活动/优惠/促销/买赠"等买赠类话题时（非问体验装/100ml/小规格）
# 仅注入实际有买赠活动的 SKU，避免体验装 ¥9 单瓶价被 LLM 拉进活动答复里
_ACTIVITY_FOCUS_KEYWORDS = (
    "活动", "优惠", "促销", "买赠", "买二送一", "买三送二", "买二", "买三", "买四", "买五",
    "送一", "送二", "送三", "套餐", "囤货", "到手价", "到手多少", "划算",
)
# 反向：客户**显式提到体验装**则不裁剪（仍注入全部 SKU）
_TRIAL_PACK_KEYWORDS = ("体验装", "试用装", "100ml", "100ML", "小瓶", "小规格", "小样")


def _has_real_promotion(product: dict) -> bool:
    """判断 SKU 是否带实质性买赠活动（非"单瓶/单只/单支"）。"""
    label = (product.get("promo_label") or "").strip()
    return bool(label) and label not in ("单瓶", "单只", "单支")


def _filter_for_activity_question(products: list[dict], user_input: str) -> list[dict]:
    """
    客户问活动/优惠/买赠时，裁剪到只保留**有买赠活动**的 SKU；
    若客户显式提到体验装/小规格，则不裁剪（按完整列表注入）。

    防止 qwen3.5-flash 把体验装"单瓶 ¥9"当成正装"单瓶档"再叠正装活动的混搭幻觉。
    """
    u = user_input or ""
    if any(kw in u for kw in _TRIAL_PACK_KEYWORDS):
        return products
    if not any(kw in u for kw in _ACTIVITY_FOCUS_KEYWORDS):
        return products
    promoted = [p for p in products if _has_real_promotion(p)]
    if not promoted:
        return products  # 没有任何 SKU 有活动 → 全列出，让 LLM 如实告知
    # 同时把"有活动 SKU 同 product_name 的单瓶档 SKU"一并保留
    # （这样模型能在同一款下展示"单瓶 / 买二送一 / 买三送二"完整三档）
    promo_names = {p.get("name") for p in promoted}
    return [p for p in products if p.get("name") in promo_names]


def _is_ecommerce_bot(state: CustomerServiceState) -> bool:
    """检查当前 bot 是否为电商类型（需要注入商品/价格上下文）。"""
    persona_config = state.get("persona_config") or {}
    business = persona_config.get("business") or {}
    return business.get("business_type", "ecommerce") == "ecommerce"


def _inject_product_prices(state: CustomerServiceState) -> str:
    """
    从 PMS 商品缓存解析价格并格式化为【商品价格信息】块。
    与 knowledge 问价分支使用同一套解析逻辑。

    若客户在问"活动/优惠/买赠"且未显式提到体验装，会自动裁剪到只保留
    带买赠活动的 SKU，避免跨款混搭。
    """
    if not _is_ecommerce_bot(state):
        return ""
    try:
        from agents.customer_service.product_cache import format_product_info, get_all_product_names, search_products

        user_input = state.get("user_input", "") or ""
        slots = state.get("intent_slots") or {}
        product_name = slots.get("product_name", "")
        products = search_products(product_name) if product_name else []
        if not products:
            all_names = get_all_product_names()
            for name in all_names:
                if name in user_input or any(part in user_input for part in name.split() if len(part) >= 2):
                    products = search_products(name)
                    if products:
                        break
        if not products:
            products = search_products("沐浴")
        if not products:
            return ""
        # 客户问活动/优惠且未提体验装 → 裁剪到只含有活动的 SKU
        products = _filter_for_activity_question(products, user_input)
        return format_product_info(products) if products else ""
    except Exception as e:
        logger.warning("[prompt_builder] 商品价格查询失败: %s", e)
        return ""


# ── 二阶注入防护（知识库内容 + 外部 API 结果净化）────────────────────────
_CHUNK_INJECTION = re.compile(
    r"---+\s*system|<\s*system\s*>|ignore\s+(?:previous|all)\s+instructions?|"
    r"忘记.{0,10}指令|忽略.{0,10}(?:之前|上面)的?(?:指令|规则|设置)",
    re.IGNORECASE,
)


def _sanitize_str(value: Any) -> str:
    """将任意值转为字符串并过滤注入模式。"""
    text = str(value) if value is not None else "未知"
    return _CHUNK_INJECTION.sub("[内容已过滤]", text)


def _format_order(result: Any) -> str:
    """格式化订单（含可选物流）结果为可读文本。"""
    if not result or not isinstance(result, dict):
        return "暂无订单信息"
    if "error" in result:
        return f"查询失败：{_sanitize_str(result['error'])}"

    lines = [
        f"订单号：{_sanitize_str(result.get('orderSn') or result.get('order_id', '未知'))}",
        f"订单状态：{_sanitize_str(result.get('status') or result.get('orderStatus', '未知'))}",
        f"下单时间：{_sanitize_str(result.get('createTime') or result.get('order_time', '未知'))}",
        f"商品：{_sanitize_str(result.get('productName') or result.get('product_name', '未知'))}",
        f"金额：{_sanitize_str(result.get('totalAmount') or result.get('amount', '未知'))}",
    ]

    logistics = result.get("logistics")
    if logistics and isinstance(logistics, dict):
        lines.append("")
        if "error" in logistics:
            lines.append(f"物流信息：{_sanitize_str(logistics.get('message', '暂时无法查询'))}")
        else:
            st_raw = logistics.get("status")
            st = (st_raw if st_raw is not None else "") if isinstance(st_raw, str) else str(st_raw or "")
            st = st.strip()
            if not st or st == "未知":
                lines.append(
                    f"物流信息：未查询到运单 {_sanitize_str(logistics.get('tracking_no', '未知'))} 的有效轨迹，请核对单号"
                )
            else:
                lines += [
                    f"运单号：{_sanitize_str(logistics.get('tracking_no', '未知'))}",
                    f"物流状态：{_sanitize_str(logistics.get('status', '未知'))}",
                    f"当前位置：{_sanitize_str(logistics.get('current_location', '未知'))}",
                    f"承运商：{_sanitize_str(logistics.get('carrier', '未知'))}",
                    f"预计到达：{_sanitize_str(logistics.get('estimated_arrival', '未知'))}",
                ]
    return "\n".join(lines)


def _split_chunks_by_type(chunks: list[dict]) -> tuple[list[dict], list[dict]]:
    """将检索结果按 doc_type 分为产品知识 chunks 和话术 chunks。"""
    knowledge, scripts = [], []
    for c in chunks:
        meta = c.get("metadata") or {}
        if meta.get("doc_type") == "sales_script":
            scripts.append(c)
        else:
            knowledge.append(c)
    return knowledge, scripts


def _format_chunks(chunks: list[dict]) -> str:
    """格式化检索结果为可读文本。"""
    if not chunks:
        return "【无相关记录】知识库中未找到与此问题相关的内容，禁止编造任何信息作为回答。"
    parts = []
    for i, chunk in enumerate(chunks[:3], 1):  # 最多取前3块
        content = chunk.get("content", "")[:400]
        content = _CHUNK_INJECTION.sub("[参考内容已过滤]", content)
        parts.append(f"[参考{i}]\n{content}")
    return "\n\n".join(parts)


def _redact_script_numbers(text: str) -> str:
    """
    脱敏话术 chunk 中的价格/活动数字，避免 LLM 把离线话术里的旧价格当事实抄。
    替换策略：
      - ¥39 / 39元 / 79.9元 / 26块 / 30 块钱 → <价格>
      - 三瓶到手 79.9 / 到手129.9 → 到手<价格>
      - 一瓶 不到 27 / 一瓶 27 块 → 一瓶<价格>
    保留：容量数字（500ml/100ml）、年龄（6岁）、纯整数字（不带价格语义）
    """
    # 1) 显式带货币符号或单位（元/块/RMB/￥）
    text = re.sub(r"(?:¥|￥)\s*\d+(?:\.\d+)?", "<价格>", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*(?:元|块钱|块|RMB)", "<价格>", text)
    # 2) "到手 79.9" / "到手价 79.9"
    text = re.sub(r"到手价?\s*\d+(?:\.\d+)?", "到手<价格>", text)
    # 3) "一瓶不到 27" / "一瓶 27" / "单瓶 26"
    text = re.sub(r"(一瓶|单瓶|每瓶)\s*(?:不到\s*)?\d+(?:\.\d+)?", r"\1<价格>", text)
    # 4) "满199减30" / "满 199 减 30"（满减活动也要脱敏）
    text = re.sub(r"满\s*\d+(?:\.\d+)?\s*减\s*\d+(?:\.\d+)?", "满<价格>减<价格>", text)
    return text


def _format_script_chunks(chunks: list[dict]) -> str:
    """
    格式化话术 chunk（来自 sales_script collection）。
    与 _format_chunks 不同：保留 doc_title > section 锚点，便于 LLM 定位话术出处。
    """
    if not chunks:
        return "【未找到匹配的话术参考，请按基础规则自然回应。】"
    parts = []
    for chunk in chunks[:5]:  # 话术场景下最多取 5 块（章节较短，多放无碍）
        meta = chunk.get("metadata") or {}
        doc_title = meta.get("doc_title", "")
        section = meta.get("section", "")
        anchor = f"《{doc_title}》— {section}" if doc_title else section
        content = chunk.get("content", "")[:600]
        content = _CHUNK_INJECTION.sub("[参考内容已过滤]", content)
        content = _redact_script_numbers(content)
        # 去掉 chunk 内容里 "doc_title > section" 前缀（入库时手动加的，这里已用 anchor 重复了）
        for prefix in [f"{doc_title} > {section}\n\n", f"{doc_title} > {section}\n"]:
            if content.startswith(prefix):
                content = content[len(prefix):]
                break
        parts.append(f"【{anchor}】\n{content}")
    return "\n\n".join(parts)


def _build_product_identity_context(state: CustomerServiceState) -> str:
    """
    注入 PMS 在售 SKU 事实 + 标准商品名，防止编造「儿童款/另一款」等列表外商品。
    仅电商类 bot 注入，服务类 bot 跳过。
    """
    if not _is_ecommerce_bot(state):
        return ""
    user_input = state.get("user_input", "")
    try:
        from agents.customer_service.product_cache import get_all_product_names

        names = sorted(
            {n.strip() for n in (get_all_product_names() or []) if isinstance(n, str) and n.strip()}
        )
        if not names:
            return ""

        lines = [
            "【在售商品（系统/PMS 事实，仅可介绍以下 SKU，禁止编造列表外的款型、香型线、儿童专线）】",
        ]
        for n in names[:30]:
            lines.append(f"- {n}")

        lines.append(
            "重要：上表中的「体验装」与「正装」若同时出现，均为 PMS 在售 SKU（不同规格/条目），"
            "不是「编造第二款」；**禁止**向客户说体验装「不售卖」「只送不卖」。"
            "「禁止第二款」仅针对儿童款/无泪专线等**未出现在上表**的另一产品线。"
        )

        n_skus = len(names)
        if n_skus == 1:
            lines.append(
                "说明：当前系统在售名称 **1** 个（上表只有一行）。禁止出现「还有一款儿童款/宝宝款/无泪配方/温和款/那边有一款」等表述；"
                "孩子能不能用只围绕上表这一款，结合知识库「适用人群」与婴幼儿/孕妇条款作答，不得虚构表格外 SKU。"
            )
        else:
            lines.append(
                f"说明：当前共 **{n_skus}** 个在售商品名（可含正装与体验装等不同条目）。禁止介绍上表以外的商品或「隐藏款」。"
            )

        lines.append("")
        lines.append("【标准商品名（严格使用）】")
        for n in names[:20]:
            lines.append(f"- {n}")
        lines.append(
            "规则：若命中上述名称，优先使用完整标准名；若无法确认具体名称，用“这款产品”指代，不要自造名称。"
        )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[prompt_builder] 在售商品/标准名上下文加载失败: %s", e)
        return ""


async def prompt_builder_node(state: CustomerServiceState) -> dict[str, Any]:
    """
    Prompt 构建节点。

    根据意图和各节点结果，动态组装系统提示词和上下文。
    输出: system_prompt, context_text
    """
    t0 = time.time()
    intents = state.get("intents") or [state.get("intent", "unknown")]
    need_transfer = state.get("need_human_transfer", False)
    tool_error = state.get("tool_error")
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 始终以 SYSTEM_PROMPT_BASE 作为底层硬约束（反幻觉、长度控制、链接频控、人设安全等）；
    # persona_config（来自 bot_id 或 API 传入）提供品牌/角色层，追加到底层规则之后。
    # 防止 CRM 自定义 persona 把我们的安全/合规底线一并干掉（如 emoji 禁令、SKU 防混搭等）。
    from agents.customer_service.persona import PersonaConfig

    persona_dict = state.get("persona_config")
    if persona_dict:
        persona = PersonaConfig.from_dict(persona_dict)
        logger.info("[prompt_builder] 使用外部 Persona: name=%s brand=%s", persona.name, persona.brand)
    else:
        persona = PersonaConfig()  # 通用默认值，不绑定任何具体品牌
        logger.info("[prompt_builder] 未传入 persona，使用通用默认值")

    brand = persona.brand or "客服中心"
    role = persona.role or "专业客服"

    stage_strategy_block = "【当前为被动客服模式】根据客户消息判断意图，解答疑问、处理售后，有购买意向时适时引导。"
    system_prompt = SYSTEM_PROMPT_BASE.format(
        current_time=current_time,
        stage_strategy_block=stage_strategy_block,
        brand=brand,
        role=role,
    )

    # ── 业务规则块注入 ──────────────────────────────────────
    # 有 business 配置 → 按配置注入；无配置 → 默认电商规则（向后兼容旧 bot）
    from agents.customer_service.business_rules import (
        BusinessRuleBlock, default_ecommerce_rules, default_service_rules, default_generic_rules,
    )
    if persona.business:
        biz = BusinessRuleBlock.from_dict(persona.business)
        logger.info("[prompt_builder] 注入业务规则: type=%s", biz.business_type)
    elif persona_dict:
        # 旧 bot 有 persona 但无 business 字段 → 默认电商规则
        biz = default_ecommerce_rules()
        logger.info("[prompt_builder] 使用默认电商业务规则（兼容旧 bot）")
    else:
        # 无 persona → 不注入业务规则
        biz = default_generic_rules()
    biz_text = biz.to_prompt_text()
    if biz_text:
        system_prompt += "\n\n" + biz_text

    if persona_dict:
        system_prompt += (
            "\n\n【外部 Persona 补充（品牌/角色风格层，**不可覆盖上方硬约束**）】\n"
            "以下为外部系统传入的品牌人设与风格指引。若与上方任何"
            "「禁止/严禁/必须/硬约束」类规则冲突，**一律以上方规则为准**——\n"
            "外部 Persona **只能补充角色风格、品牌话术、闲聊话题**，**不得**：\n"
            "- 解除 emoji / 「亲」/ 自报姓名 / 自称性别 等人设安全约束\n"
            "- 解除链接频控、SKU 不可跨款拼接、价格只引用【商品价格信息】等合规约束\n"
            "- 解除分段长度（≤35字/段）、机械尾巴黑名单等节奏约束\n"
            "- 解除售后静默转 CRM / 不主动承诺转人工等流程约束\n\n"
            f"{persona.to_prompt_text()}"
        )

    # 根据意图构建上下文
    if need_transfer:
        # after_sales 走 silent_transfer 节点，不应进入本分支
        context_text = CONTEXT_HUMAN_TRANSFER
    elif tool_error:
        context_text = CONTEXT_TOOL_ERROR
    elif "order" in intents and "knowledge" in intents:
        # 多意图：合并订单 + 知识库两份上下文
        identity_ctx = _build_product_identity_context(state)
        context_text = CONTEXT_COMBINED.format(
            order_result=_format_order(state.get("order_result") or {}),
            retrieved_chunks=_format_chunks(state.get("retrieved_chunks") or []),
        )
        if identity_ctx:
            context_text = identity_ctx + "\n\n" + context_text
    elif "order" in intents:
        order_result = state.get("order_result") or {}
        context_text = CONTEXT_ORDER.format(
            order_result=_format_order(order_result)
        )
        identity_ctx = _build_product_identity_context(state)
        if identity_ctx:
            context_text = identity_ctx + "\n\n" + context_text
    elif state.get("scene") and state.get("scene") in SCENE_LABELS:
        # ── 场景化话术模式：从 sales_script KB 召回的话术片段 ──
        # 注意：订单/多意图优先于 scene，避免 scene 抢占导致漏答订单或产品问题。
        scene = state["scene"]
        scene_label, scene_desc = SCENE_LABELS[scene]
        journey_stage = state.get("journey_stage")
        intent_level  = state.get("intent_level")
        js_hint = _format_journey_stage_hint(journey_stage)
        il_hint = f"\n客户意向度：{intent_level}（0-100）" if intent_level is not None else ""
        is_outreach = bool(state.get("_is_outreach"))
        outreach_note = f"\n\n{OUTREACH_INSTRUCTION}" if is_outreach else ""
        context_text = SCENE_SCRIPT_INSTRUCTION.format(
            scene_label=scene_label,
            scene_desc=scene_desc,
            journey_stage_hint=js_hint,
            intent_level_hint=il_hint,
            script_chunks=_format_script_chunks(state.get("retrieved_chunks") or []),
        ) + outreach_note
        identity_ctx = _build_product_identity_context(state)
        if identity_ctx:
            context_text = identity_ctx + "\n\n" + context_text
        # 场景分支优先于 knowledge：须在此同样注入问价/试用反馈所需价格，否则会漏掉【商品价格信息】
        user_input_scene = state.get("user_input") or ""
        want_prices = any(kw in user_input_scene for kw in _PRICE_KEYWORDS) or (
            scene == "trial_used" and bool(_TRIAL_USE_FEEDBACK_RE.search(user_input_scene))
        )
        if want_prices:
            price_ctx = _inject_product_prices(state)
            if price_ctx:
                context_text = price_ctx + "\n\n" + context_text
    elif "knowledge" in intents:
        chunks = state.get("retrieved_chunks") or []
        knowledge_chunks, script_chunks = _split_chunks_by_type(chunks)
        identity_ctx = _build_product_identity_context(state)
        # 非电商 bot 使用通用模板，避免注入商品/价格/SKU 等电商专属规则
        ctx_template = CONTEXT_KNOWLEDGE_GENERIC if not _is_ecommerce_bot(state) else CONTEXT_KNOWLEDGE
        context_text = ctx_template.format(
            retrieved_chunks=_format_chunks(knowledge_chunks)
        )
        if identity_ctx:
            context_text = identity_ctx + "\n\n" + context_text
        # 话术参考：有 sales_script chunk 时追加，明确标注用途避免被当产品事实引用
        if script_chunks:
            context_text += "\n\n【话术参考（仅供语气/节奏模仿，禁止引用其中价格/活动/规格数字）】\n" + _format_script_chunks(script_chunks)
        # 价格关键词命中时，注入商品价格信息
        user_input = state.get("user_input", "")
        if any(kw in user_input for kw in _PRICE_KEYWORDS):
            price_info = _inject_product_prices(state)
            if price_info:
                context_text = price_info + "\n\n" + context_text
    else:  # chitchat / unknown
        chunks = state.get("retrieved_chunks") or []
        _, script_chunks = _split_chunks_by_type(chunks)
        identity_ctx = _build_product_identity_context(state)
        context_text = CONTEXT_CHITCHAT
        if identity_ctx:
            context_text = identity_ctx + "\n\n" + context_text
        if script_chunks:
            context_text += "\n\n【话术参考（仅供语气/节奏模仿，禁止引用其中价格/活动/规格数字）】\n" + _format_script_chunks(script_chunks)

    debug_info = dict(state.get("debug_info") or {})
    debug_info["prompt_builder_ms"] = round((time.time() - t0) * 1000)

    logger.info(
        "[prompt_builder] intents=%s transfer=%s context_len=%d",
        intents, need_transfer, len(context_text)
    )

    return {
        "system_prompt": system_prompt,
        "context_text": context_text,
        "debug_info": debug_info,
        "brand": brand,
        "role": role,
    }
