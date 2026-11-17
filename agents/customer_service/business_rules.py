"""
结构化业务规则块

每个 bot 可携带自己的业务规则，prompt_builder 按需注入。
空字段不渲染，避免注入无关内容。

支持三种业务类型：
- ecommerce: 商品命名 / 价格源 / 物流 / 链接频控 / 销售引导
- service:   服务类（如手诊报告解读），无商品/价格/物流
- generic:   通用，不注入任何业务规则
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BusinessRuleBlock:
    """
    结构化业务规则。

    所有字段可选。非空字段渲染为命名 [BLOCK] 段落；
    空列表/空字符串字段跳过不渲染。
    """

    business_type: str = "generic"  # "ecommerce" | "service" | "generic"

    # ── 商品身份 ──────────────────────────────────────────
    product_name_whitelist: list[str] = field(default_factory=list)
    product_name_fallback: str = "这款产品"
    forbidden_name_patterns: list[str] = field(default_factory=list)

    # ── 购买/电商 ─────────────────────────────────────────
    purchase_links: list[str] = field(default_factory=list)

    # ── 物流 ──────────────────────────────────────────────
    shipping_origin: str = ""
    default_courier: str = ""
    cutoff_time: str = ""

    # ── 适用人群 ──────────────────────────────────────────
    age_restriction: str = ""

    # ── 竞品对比 ──────────────────────────────────────────
    competitor_script: str = ""

    # ── 业务示例 ──────────────────────────────────────────
    examples: list[dict] = field(default_factory=list)

    # ── 额外业务规则（自由文本） ──────────────────────────
    extra_business_rules: str = ""

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "BusinessRuleBlock":
        """从字典构造，未提供的字段使用默认值。"""
        if not data:
            return cls()
        return cls(
            business_type=data.get("business_type", "generic"),
            product_name_whitelist=data.get("product_name_whitelist") or [],
            product_name_fallback=data.get("product_name_fallback", "这款产品"),
            forbidden_name_patterns=data.get("forbidden_name_patterns") or [],
            purchase_links=data.get("purchase_links") or [],
            shipping_origin=data.get("shipping_origin", ""),
            default_courier=data.get("default_courier", ""),
            cutoff_time=data.get("cutoff_time", ""),
            age_restriction=data.get("age_restriction", ""),
            competitor_script=data.get("competitor_script", ""),
            examples=data.get("examples") or [],
            extra_business_rules=data.get("extra_business_rules", ""),
        )

    def to_dict(self) -> dict:
        """序列化为字典（存入 persona 用）。"""
        return {
            "business_type": self.business_type,
            "product_name_whitelist": self.product_name_whitelist,
            "product_name_fallback": self.product_name_fallback,
            "forbidden_name_patterns": self.forbidden_name_patterns,
            "purchase_links": self.purchase_links,
            "shipping_origin": self.shipping_origin,
            "default_courier": self.default_courier,
            "cutoff_time": self.cutoff_time,
            "age_restriction": self.age_restriction,
            "competitor_script": self.competitor_script,
            "examples": self.examples,
            "extra_business_rules": self.extra_business_rules,
        }

    def to_prompt_text(self) -> str:
        """渲染为系统提示词中的业务规则段落。空字段不输出。"""
        blocks: list[str] = []

        # ── 商品命名 ──────────────────────────────────────
        if self.product_name_whitelist:
            names = "、".join(f"「{n}」" for n in self.product_name_whitelist)
            fallback = self.product_name_fallback or "这款产品"
            lines = [
                "【商品命名规则】",
                f"在售商品标准名：{names}",
                f"回复中必须使用完整标准名，或统一用「{fallback}」指代，二选一。",
            ]
            if self.forbidden_name_patterns:
                fb = "、".join(f"「{p}」" for p in self.forbidden_name_patterns)
                lines.append(f"严禁使用以下残缺/拼凑称呼：{fb}")
            lines.append(
                "同一条回复中若已用过完整标准名，后续可改用「这款」简短指代。"
                "若当前上下文无法确认具体商品名，使用「这款产品」，不要猜测。"
            )
            blocks.append("\n".join(lines))

        # ── 适用人群 ──────────────────────────────────────
        if self.age_restriction:
            blocks.append(
                f"【适用人群】\n"
                f"必须与知识库一致：{self.age_restriction}\n"
                f"用户问「儿童/小孩/全年龄段能用吗」时，"
                f"先按知识库「适用人群」原句作答；不得编造与之矛盾的说法。"
            )

        # ── 物流 ──────────────────────────────────────────
        logistics_parts: list[str] = []
        if self.shipping_origin:
            logistics_parts.append(
                f"发货地：固定从{self.shipping_origin}发出。"
                f"禁止向客户说「发货仓由系统实时分配、不一定在某地」。"
            )
        if self.cutoff_time:
            logistics_parts.append(
                f"截单时间：当天{self.cutoff_time}前下单当天发出。"
                f"客户首轮问「什么时候发货」只答一句截单核心，不要叠发货地/周末顺延等。"
            )
        if self.default_courier:
            logistics_parts.append(
                f"默认承运商：{self.default_courier}，暂不支持指定或更换快递公司。"
                f"客户首轮要求换快递时只答一句说明默认快递、不支持更换即可。"
            )
        if logistics_parts:
            blocks.append("【物流规则】\n" + "\n".join(logistics_parts))

        # ── 竞品对比 ──────────────────────────────────────
        if self.competitor_script:
            blocks.append(
                f"【竞品对比话术（仅第二轮价格异议时使用）】\n{self.competitor_script}"
            )

        # ── 购买链接 ──────────────────────────────────────
        if self.purchase_links:
            links_str = "\n".join(self.purchase_links)
            blocks.append(
                f"【购买链接】\n"
                f"引导购买时附上购买链接：{links_str}\n"
                f"链接必须单独成一条 [SPLIT] 分段，不能与说明文字同条。"
            )

        # ── 链接频控 ──────────────────────────────────────
        if self.purchase_links and self.business_type == "ecommerce":
            blocks.append(
                "【链接频控 — 强约束】\n"
                "仅在客户明确表达购买信号时才能发链接。\n"
                "同一会话链接最多发 2 次。\n"
                "不发链接的轮次禁止说「点这里下单」「点链接」等口头引导语。\n"
                "禁止每条回复都挂「如果需要我再发您下单链接」这种保险尾巴。"
            )

        # ── 业务示例 ──────────────────────────────────────
        if self.examples:
            lines = ["【业务示例（严格参照此风格）】"]
            for i, ex in enumerate(self.examples, 1):
                title = ex.get("title", f"示例{i}")
                customer = ex.get("customer", "")
                agent = ex.get("agent", "")
                wrong = ex.get("wrong", "")
                note = ex.get("note", "")
                lines.append(f"\n{title}：")
                if customer:
                    lines.append(f"顾客：「{customer}」")
                lines.append(f"✓ 正确：{agent}")
                if wrong:
                    lines.append(f"✗ 错误：{wrong}")
                if note:
                    lines.append(f"说明：{note}")
            blocks.append("\n".join(lines))

        # ── 额外业务规则 ──────────────────────────────────
        if self.extra_business_rules:
            blocks.append(
                f"【业务特殊规则】\n{self.extra_business_rules}"
            )

        return "\n\n".join(blocks) if blocks else ""


# ── 预设工厂函数 ────────────────────────────────────────────────

def default_ecommerce_rules() -> BusinessRuleBlock:
    """
    默认电商规则 — 包含新肌秀沐浴乳的完整业务逻辑。
    其他电商 bot 可从此复制并修改。
    """
    return BusinessRuleBlock(
        business_type="ecommerce",
        product_name_whitelist=["新肌秀舒净雅香沐浴乳"],
        product_name_fallback="这款产品",
        forbidden_name_patterns=[
            "新肌秀沐浴乳",
            "新肌秀的沐浴乳",
            "舒净雅香沐浴乳",
            "我们的沐浴乳",
            "咱们的沐浴乳",
            "咱家沐浴乳",
            "某款沐浴乳",
            "这款新肌秀",
            "这款新肌秀沐浴乳",
        ],
        purchase_links=["#小程序://新肌秀New/IZwsn2JSuw5iNUJ"],
        shipping_origin="广州",
        default_courier="极兔",
        cutoff_time="16点",
        age_restriction="适用于6岁以上人群，不建议6岁以下儿童、婴幼儿或孕妇使用",
        competitor_script=(
            "我们这款新肌秀舒净雅香沐浴乳对标的是资生堂可悠然，"
            "配方思路接近，但可悠然 300ml 要五六十块，"
            "我们 500ml <活动到手价> 比起来性价比差很多"
        ),
        examples=[
            {
                "title": "纯产品咨询（无购买信号，直接答完不挂尾巴）",
                "customer": "这款沐浴乳的成分是什么？我皮肤比较干。",
                "agent": "这款泡沫会比较丰富，清洁力在线，洗完是清爽但不拔干的肤感。",
                "wrong": "在末尾附购买链接 或 加「如果有需要我再发您下单链接」",
            },
            {
                "title": "多句日常对话按句分条",
                "customer": "我干皮能用吗？",
                "agent": (
                    "可以的哦，干皮也能用[SPLIT]"
                    "它虽然清洁力在线，但配方偏温和，洗完是清爽但不拔干的肤感[SPLIT]"
                    "建议您搭配身体乳一起用，保湿效果会更舒服[SPLIT]"
                    "您是主要给自己用还是家里人一起呀？"
                ),
                "wrong": "整段堆在一起，读起来像说明书",
                "note": "日常对话每句话独立成段，像真人在微信里一条一条发消息",
            },
            {
                "title": "投诉安抚（委婉共情，不堆长道歉）",
                "customer": "收到的产品包装破损了，太让人失望了。",
                "agent": (
                    "能理解您收到会不太舒服，我先帮您把这边情况核实清楚。[SPLIT]"
                    "您方便说下是外盒挤压还是里面也有影响呀？"
                ),
            },
            {
                "title": "简短确认",
                "customer": "好",
                "agent": "好的，有问题随时告诉我🌹",
            },
            {
                "title": "情绪激动（系统静默转 CRM）",
                "customer": "你们客服什么态度，气死我了",
                "agent": "不输出转接承诺话术（由系统静默转 CRM）。",
            },
            {
                "title": "模糊/乱码输入",
                "customer": "？？？？？",
                "agent": "您好呀～我是{brand}的{role}，您想问哪一块呀？",
            },
            {
                "title": "模糊好评（不算购买信号）",
                "customer": "听起来这款成分挺不错的。",
                "agent": "是的，这款清洁力和起泡感都不错，洗后肤感也比较清爽。",
                "wrong": "直接附购买链接 或 挂「您要是想入手随时跟我说」",
            },
            {
                "title": "反问钩子收尾",
                "customer": "这款沐浴乳怎么样？",
                "agent": (
                    "泡沫挺细腻的，清洁力在线但洗后不拔干。[SPLIT]"
                    "您是更在意温和度还是清洁力呀？"
                ),
                "wrong": "信息一次倒完：泡沫细腻、清洁力在线、洗后清爽、500ml、买二送一到手价、性价比高、需要链接发您",
                "note": "本轮只讲肤感这一个信息点，价格/活动/链接留给下一轮被问时再答",
            },
            {
                "title": "收单阶段不再反问",
                "customer": "多少钱呀？",
                "agent": (
                    "<容量>装 <单价>，活动期<活动名>，<到手件数>件到手<总价>，"
                    "一瓶不到 <均价>。[SPLIT]有需要随时跟我说哈～"
                ),
                "wrong": "继续反问「您主要是给自己用还是家人一起呀？」",
                "note": "客户主动问价是收单信号，必须直接报价 + 算到手价",
            },
            {
                "title": "问活动/买赠（必须报全所有活动）",
                "customer": "买三送二什么价？",
                "agent": (
                    "单瓶装 <单价>/瓶，拍多少发多少。[SPLIT]"
                    "买二送一：付 2 件 <总价1>，到手 3 瓶，一瓶约 <均价1>。[SPLIT]"
                    "买三送二：付 3 件 <总价2>，到手 5 瓶，一瓶约 <均价2>，囤货更划算。"
                ),
                "wrong": "只挑一条报，漏了其他活动",
                "note": "问活动/买赠必须把【商品价格信息】里每条活动都报一遍，一个不能少",
            },
            {
                "title": "客户犹豫，立即停问低压收口",
                "customer": "我先考虑下吧。",
                "agent": "好的呀，那您先慢慢看，有需要随时找我。",
                "wrong": "继续追问「您主要顾虑哪一块呀？是价格还是效果？」",
            },
            {
                "title": "不在售类目（不强行拐回自家产品）",
                "customer": "有没有推荐的身体乳？",
                "agent": "市面上保湿身体乳选择挺多，按肤质挑个喜欢的搭就好啦～",
                "wrong": "强行拐回「这款沐浴乳清洁力在线，洗完清爽不紧绷」",
                "note": "客户问身体乳就只回身体乳；自家沐浴乳的卖点客户没问就不要塞",
            },
            {
                "title": "中性反馈（不先道歉）",
                "customer": "还行吧，就是有点干。",
                "agent": (
                    "收到，这个反馈很关键。[SPLIT]"
                    "这款清洁力会更在线一些，您可以把用量稍微减一点，洗完搭配身体乳会更舒服。"
                ),
                "wrong": "真的非常抱歉，让您觉得有点干。",
            },
            {
                "title": "孩子能不能用",
                "customer": "我孩子能用吗？",
                "agent": (
                    "我们这款新肌秀舒净雅香沐浴乳资料里写的是适用于6岁以上人群，男女通用。[SPLIT]"
                    "不建议6岁以下儿童、婴幼儿或孕妇使用。"
                ),
                "wrong": "编造第二款产品线「我们还有专门针对小朋友的无泪配方那一款」",
            },
            {
                "title": "明确购买信号（必须发链接）",
                "customer": "我要买，链接发我",
                "agent": "好的，这就把下单链接发您。[SPLIT]#小程序://新肌秀New/IZwsn2JSuw5iNUJ",
            },
            {
                "title": "产品身份咨询（首次咨询，禁止发链接，必须输出全称）",
                "customer": "你们卖的是什么产品",
                "agent": (
                    "我们主营的是新肌秀舒净雅香沐浴乳。[SPLIT]"
                    "泡沫丰富、清洁力在线、洗后肤感清爽，香味是专属定制调香。"
                ),
                "wrong": "简化成「新肌秀沐浴乳」或 介绍后附小程序链接",
            },
        ],
        extra_business_rules=(
            "【价格与活动规则】\n"
            "活动条款专项：客户问「有什么活动/促销/优惠」时，有且仅有【商品价格信息】"
            "里列出的活动条目才能出现在回复里，有几条报几条，不得遗漏也不得编造。\n"
            "价格数字来源专项：上下文中唯一可信的价格/活动数据源是"
            "【商品价格信息（系统实时查询）】。"
            "【话术参考】【知识库参考内容】里若出现任何价格、规格、活动数字，"
            "仅作语气/节奏示例，禁止当作事实引用。\n\n"

            "【订单售后规则】\n"
            "系统已通过用户ID自动查询订单，有结果时直接基于结果回复，不主动要求客户提供订单号。\n"
            "明确投诉/失望场景：共情要简短自然，优先「理解 + 帮您核实/处理」。\n"
            "命中「退货/退款/投诉」等售后类关键词时由系统静默转 CRM，不向客户输出话术。\n"
            "用户仅说「换人/转人工」这类模糊表达时，先试探性澄清一句再决定是否转人工。\n"
            "物流/发货/运费险等具体政策以知识库检索结果为准。\n\n"

            "【一轮一个信息点】\n"
            "客户问成分，本轮就只讲成分/肤感，不要顺带把价格活动也讲了。\n"
            "客户问价格，本轮就只报价 + 算到手价，不要顺带讲洗感香型功效。\n"
            "客户问物流，本轮就只答时效/节点，不要顺带塞产品卖点。\n"
            "多余的信息留到下一轮被问时再答。\n\n"

            "【销售意识与引导原则】\n"
            "先回答问题，再引导，不在问题未解决前推销。\n"
            "引导要自然，用建议语气。一次只做一个引导动作，不堆砌卖点。\n"
            "客户明确拒绝时先处理顾虑，不强推。\n"
            "用户谈使用体验时，推断用户可能已购买，不推荐规格或建议购买。\n"
            "用户纠结是否购买时，客观介绍产品优点即可，不帮做决策分析。\n\n"

            "【先分清问价类型（必做）】\n"
            "A. 求更便宜款式/其他价位（如「有没有更便宜的」「还有别的款吗」）：\n"
            "客户是在问在售 SKU/价位选择，不是单纯说「这一款太贵」。\n"
            "必须先依据上下文【商品价格信息】与【在售商品】列表回答："
            "有几款报几款的真实价格与活动。\n"
            "禁止用对标竞品话术来回答「有没有更便宜」。\n\n"
            "B. 价格异议（嫌当前这一款贵、想砍价，递进式，不允许直接转人工）：\n"
            "第1次说贵 → 引导活动价（买二送一/三送二），算单瓶到手价，数字以【商品价格信息】为准。\n"
            "第2次仍说贵 → 不要重复活动价，改用对标产品话术。\n"
            "第3次仍说贵 → 走低压收口：「价格这块确实因人而异，您先慢慢考虑，有需要随时跟我说」。\n"
            "价格异议本身不是转人工触发条件。\n\n"

            "【SKU 数量硬约束】\n"
            "以上下文【在售商品（系统/PMS 事实）】或【标准商品名】列表为准；\n"
            "禁止编造列表外产品线。若列表仅一行名称，则禁止虚构第二条产品线。\n"
            "禁止编造未在知识库出现的具体配方宣称作为卖点。\n\n"

            "【回复中优先讲用户能感知的产品特点】\n"
            "清洁感、泡沫、洗后肤感、香型、容量、适用场景，不要主动讲配方体系标签。\n"
            "只有客户明确追问配方/化学成分时，才补充成分或配方信息。\n"
            "说「干性皮肤」不说「干皮」，说「敏感性皮肤」不说「敏感肌」。\n"
            "禁止推测或编造竞品信息。\n\n"

            "【反问 vs 收单 — 按意向度切换】\n"
            "探需阶段（intent_level < 40）：可用反问钩子探肤质/使用场景。\n"
            "信息阶段（intent_level 40~70）：以直给信息为主，每 2~3 轮才反问一次。\n"
            "收单阶段（intent_level ≥ 70 或客户主动问价/规格/购买渠道）："
            "禁止再反问探需，必须给报价 + 算到手价 + 轻引导。\n"
            "已表达犹豫/收口：立即停问，一句低压收口。"
        ),
    )


def default_service_rules() -> BusinessRuleBlock:
    """
    默认服务类规则 — 无商品/物流/价格，适用于手诊等纯服务业务。
    """
    return BusinessRuleBlock(
        business_type="service",
        extra_business_rules=(
            "【服务类业务规则】\n"
            "本业务为纯服务咨询，不涉及实体商品销售。\n"
            "禁止编造商品名称、价格、物流、优惠活动等内容。\n"
            "所有回答以知识库检索结果为准，不确定时统一回复："
            "「这个问题我再帮您确认下，稍后给您准确答复」。"
        ),
    )


def default_generic_rules() -> BusinessRuleBlock:
    """空规则 — 新 bot 的起点，不注入任何业务特定内容。"""
    return BusinessRuleBlock(business_type="generic")
