"""
商品价格/规格缓存模块

启动时拉全量商品列表到进程内缓存，每 10 分钟定时刷新。
客户问价格时按商品名模糊匹配，返回真实价格和规格信息。

接口地址由 settings 统一管理：
  backend_base_url + pms_product_list_path
  默认：https://sales.xinjixiu.com.cn/prod-api/pms/product/detailList
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from agent_framework.config.settings import get_settings

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000
_REFRESH_INTERVAL = 600  # 10 分钟
_TIMEOUT = 10.0


def _api_url() -> str:
    """每次调用重新读取 settings，便于运行期热更（lru_cache 实例不变）。"""
    return get_settings().pms_product_list_url

# 产品名关键词白名单：只缓存与护肤/日化相关的商品，排除测试数据
_PRODUCT_KEYWORDS = {"沐浴", "洗", "护肤", "乳液", "面膜", "精华", "防晒", "洁面", "护发", "乳霜"}
# 排除关键词：明显的测试数据或非相关品类
_EXCLUDE_KEYWORDS = {"test", "测试", "勿动", "预约", "iphone", "小米", "redmi", "oppo",
                     "华为", "三星", "电视", "手机", "笔记本", "平板", "0.01"}
# 异常低价阈值：低于该价格视为测试数据（如 ¥0.01 特价占位），不注入上下文
_MIN_VALID_PRICE = 1.0


def _positive_price(val: Any) -> float | None:
    """接口数值转为正价；None/空/≤0 视为无有效价。"""
    if val is None or val == "":
        return None
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def _looks_like_trial_or_small_spec(name: str, specs: str = "") -> bool:
    """体验装/小规格若仍无有效价，保留条目注入「暂无报价」，避免模型误以为「不售卖」。"""
    blob = f"{name}{specs}"
    if not blob.strip():
        return False
    markers = ("体验", "试用装", "小样", "100ml")
    return any(m in blob for m in markers)


# ── 进程内缓存 ────────────────────────────────────────────────
_product_cache: list[dict[str, Any]] = []
_cache_ts: float = 0.0
_refresh_task: asyncio.Task | None = None
_consecutive_failures: int = 0          # 连续刷新失败次数
_FAILURE_ALERT_THRESHOLD = 3            # 连续失败达此次数时 ERROR 级别告警


def _parse_sku(raw_product: dict, sku: dict) -> dict[str, Any]:
    """
    将单条 SKU 解析为产品记录。
    一个商品下若有多条 SKU（不同活动套餐），会被拆成多条记录注入上下文，
    让 LLM 看到「单瓶 / 买二送一 / 买三送二」并列。
    """
    specs = ""
    promo_label = ""
    sp_data = sku.get("spData")
    if sp_data and isinstance(sp_data, str):
        try:
            sp_list = json.loads(sp_data)
            spec_parts = []
            for item in sp_list:
                if not item.get("key"):
                    continue
                if item["key"] == "名称" and item.get("value"):
                    promo_label = item["value"]  # e.g. "单瓶" / "买二送一" / "买三送二"
                else:
                    spec_parts.append(f"{item['key']}:{item['value']}")
            specs = "、".join(spec_parts)
        except (json.JSONDecodeError, KeyError):
            pass

    sku_price = sku.get("price")
    sku_promo = sku.get("promotionPrice")
    product_price = raw_product.get("price")

    price = _positive_price(sku_price)
    if price is None:
        price = _positive_price(product_price)

    promotion_price = None
    promo_val = _positive_price(sku_promo)
    if promo_val is not None and (price is None or promo_val != price):
        promotion_price = promo_val

    return {
        "id": raw_product.get("id"),
        "sku_id": sku.get("id"),
        "sku_code": sku.get("skuCode"),
        "name": raw_product.get("name", ""),
        "sku_name": sku.get("productName", ""),
        "price": price,
        "promotion_price": promotion_price,
        "promo_label": promo_label,
        "specs": specs,
        "brand": raw_product.get("brandName", ""),
        "category": raw_product.get("productCategoryName", ""),
        "pic": sku.get("pic") or raw_product.get("pic", ""),
        "stock": sku.get("stock", 0),
    }


def _parse_product(raw: dict) -> list[dict[str, Any]]:
    """将接口原始数据解析为精简结构。一个商品可能含多条 SKU，每条返回一项记录。"""
    sku_list = raw.get("skuStockList") or []
    if not sku_list:
        # 退化兼容：若新端点未返回 skuStockList，回退到旧字段
        legacy = raw.get("pmsSkuStock")
        sku_list = [legacy] if isinstance(legacy, dict) else []
    if not sku_list:
        return []
    return [_parse_sku(raw, sku) for sku in sku_list if isinstance(sku, dict)]


async def _fetch_products() -> list[dict[str, Any]]:
    """从接口拉取全量商品列表，过滤后返回有效商品。"""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_api_url(), params={"pageNum": 1, "pageSize": _PAGE_SIZE})
        resp.raise_for_status()
        data = resp.json()

    rows = data.get("rows") or []
    products = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        name_lower = name.lower()
        # 排除测试数据和非相关品类
        if any(ex in name_lower for ex in _EXCLUDE_KEYWORDS):
            continue
        # 白名单匹配：名称含护肤/日化关键词才保留
        if not any(kw in name for kw in _PRODUCT_KEYWORDS):
            continue
        for p in _parse_product(r):
            # 异常低价：通常是 PMS 测试数据（如 ¥0.01 特价占位），直接丢弃，避免污染上下文
            if p["price"] is not None and p["price"] < _MIN_VALID_PRICE:
                logger.info("[product_cache] 丢弃异常低价测试数据: %s ¥%s", name, p["price"])
                continue
            # 无有效价格：普通商品丢弃；体验/小规格仍保留（注入「暂无报价」），避免模型编造「不售卖」
            if p["price"] is None:
                if _looks_like_trial_or_small_spec(name, p.get("specs") or ""):
                    logger.info("[product_cache] 保留无标价商品（疑似体验/小规格）: %s", name)
                    products.append(p)
                continue
            products.append(p)

    logger.info("[product_cache] 拉取商品列表成功: 原始%d条，过滤后%d条", len(rows), len(products))
    return products


async def refresh_cache() -> None:
    """刷新商品缓存。接口不可用时保留旧缓存并计数告警。"""
    global _product_cache, _cache_ts, _consecutive_failures
    try:
        products = await _fetch_products()
        _product_cache = products
        _cache_ts = time.time()
        if _consecutive_failures > 0:
            logger.info("[product_cache] 刷新恢复正常（之前连续失败 %d 次）", _consecutive_failures)
        _consecutive_failures = 0
    except Exception as e:
        _consecutive_failures += 1
        stale_minutes = int((time.time() - _cache_ts) / 60) if _cache_ts else -1
        if _consecutive_failures >= _FAILURE_ALERT_THRESHOLD:
            logger.error(
                "[product_cache] 连续刷新失败 %d 次，缓存已过期 %d 分钟，价格信息可能不准确: %s",
                _consecutive_failures, stale_minutes, e,
            )
        else:
            logger.warning(
                "[product_cache] 刷新失败（第%d次），保留旧缓存(%d条，过期%d分钟): %s",
                _consecutive_failures, len(_product_cache), stale_minutes, e,
            )


async def _refresh_loop() -> None:
    """后台定时刷新任务。"""
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)
        await refresh_cache()


async def init() -> None:
    """初始化：拉取一次数据 + 启动定时刷新任务。"""
    global _refresh_task
    await refresh_cache()
    _refresh_task = asyncio.create_task(_refresh_loop())
    logger.info("[product_cache] 初始化完成，已启动定时刷新（每%d秒）", _REFRESH_INTERVAL)


def stop() -> None:
    """停止定时刷新（应用关闭时调用）。"""
    global _refresh_task
    if _refresh_task:
        _refresh_task.cancel()
        _refresh_task = None


def search_products(keyword: str) -> list[dict[str, Any]]:
    """
    按关键词模糊搜索商品。
    遍历缓存中的 name 做子串匹配，返回匹配的商品列表（最多 5 条）。
    """
    if not keyword or not _product_cache:
        return []

    keyword = keyword.strip().lower()
    results = []
    for p in _product_cache:
        name = (p.get("name") or "").lower()
        sku_name = (p.get("sku_name") or "").lower()
        if keyword in name or keyword in sku_name:
            results.append(p)
            if len(results) >= 5:
                break
    return results


def get_all_product_names() -> list[str]:
    """返回所有商品名称列表，用于 LLM 辅助匹配。"""
    return list(set(p["name"] for p in _product_cache if p.get("name")))


_BUY_GIFT_RE = re.compile(r"^买([0-9一二三四五六七八九十]+)送([0-9一二三四五六七八九十]+)$")
_CN_NUM_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_qty(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    return None


def _format_promo_line(p: dict) -> str:
    """按 promo_label 渲染一行活动说明，含件数与到手价（防止 LLM 自己算错）。"""
    price = p.get("price") or 0.0
    label = (p.get("promo_label") or "").strip()
    promo_text = ""
    if label and label not in ("单瓶", "单只", "单支"):
        m = _BUY_GIFT_RE.match(label)
        if m:
            buy = _parse_qty(m.group(1))
            give = _parse_qty(m.group(2))
            if buy is not None and give is not None:
                total_qty = buy + give
                total_price = round(price * buy, 2)
                unit_price = round(total_price / total_qty, 2) if total_qty else price
                promo_text = (
                    f"【{label}】下单 {buy} 件付 ¥{_fmt_price(total_price)}，"
                    f"共到手 {total_qty} 件，平均 ¥{_fmt_price(unit_price)}/件"
                )
        if not promo_text:
            promo_text = f"【{label}】"
    elif label:
        promo_text = f"【{label}】单价 ¥{_fmt_price(price)}/件"
    return promo_text


def _fmt_price(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def format_product_info(products: list[dict]) -> str:
    """将商品列表格式化为可读文本，注入到 LLM 上下文。同一商品的多条活动合并展示。

    输出格式以「═══ 分隔线 + SKU 编号 + 商品名标题」明确划分 SKU 边界，
    防止 LLM 跨 SKU 拼接活动（如把"体验装的单瓶价"和"正装的买二送一活动"误当作同一款）。
    """
    if not products:
        return ""

    # 按商品名分组（同一 product 下的多条 SKU 合并）
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for p in products:
        key = p.get("name") or ""
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(p)

    SEP = "═" * 40
    lines = [
        "【商品价格信息（系统实时查询，唯一可信价格源）】",
        f"⚠️ 共 {len(order)} 款独立 SKU，每款活动仅适用于该款自身。**严禁跨款拼接活动**（例如不能把体验装的「单瓶价」与正装的「买二送一/买三送二」合在一起报）。",
    ]
    for idx, name in enumerate(order, 1):
        items = grouped[name]
        head = items[0]
        specs = head.get("specs", "")
        clean_specs = "、".join(
            s for s in specs.split("、") if s and not s.startswith("名称:")
        )
        unit_price = head.get("price")
        price_text = f"¥{_fmt_price(unit_price)}/件" if unit_price else "暂无报价"

        lines.append("")
        lines.append(SEP)
        sku_header = f"▼ SKU#{idx} ▼ 《{name}》"
        if clean_specs:
            sku_header += f"  规格：{clean_specs}"
        sku_header += f"  单价：{price_text}"
        lines.append(sku_header)
        lines.append("可选活动（仅本款 SKU#%d 适用）：" % idx)

        promo_lines = []
        has_real_promo = False
        for it in items:
            label = (it.get("promo_label") or "").strip()
            if label and label not in ("单瓶", "单只", "单支"):
                has_real_promo = True
            line = _format_promo_line(it)
            if line:
                promo_lines.append("  " + line)
        if not has_real_promo:
            # 无实质买赠活动：用明确措辞表达，避免"单瓶 ¥X"被 LLM 误识别为"活动档"
            lines.append("  本款无促销活动，按单价销售：" + price_text)
        elif promo_lines:
            lines.extend(promo_lines)
        else:
            lines.append("  【单瓶】单价 " + price_text)
        lines.append(f"▲ SKU#{idx} 活动到此结束 ▲")

    lines.append(SEP)
    return "\n".join(lines)
