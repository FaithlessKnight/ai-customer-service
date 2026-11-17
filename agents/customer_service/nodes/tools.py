"""
工具调用节点（含重试 + 超时 + 降级兜底）

订单查询统一流程（order 意图）：
  1. 用 userId 查订单列表
  2. 如果槽位里有 order_id，从列表中过滤对应订单
  3. 如果用户问的是物流/快递（含 tracking_no 或问句含物流关键词），
     继续用 deliverySn 查物流轨迹并合并返回

接口地址由 settings 统一管理：
  backend_base_url + oms_order_base_path
  默认：https://sales.xinjixiu.com.cn/prod-api/oms/omsOrder
    GET {base}/getOrderList/{userId}
    GET {base}/getLogisticsTrace?deliverySn=xxx
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from agent_framework.config.settings import get_settings
from agents.customer_service.state import CustomerServiceState

logger = logging.getLogger(__name__)

_TIMEOUT   = 8.0    # 单次请求超时秒数
_MAX_RETRY = 3      # 最大重试次数


def _base_url() -> str:
    """每次调用重新读取 settings，便于运行期热更。"""
    return get_settings().oms_order_base_url

# 判断是否需要追查物流的关键词
_LOGISTICS_KEYWORDS = {"快递", "物流", "运单", "包裹", "到哪", "发货", "几天", "配送", "签收"}


# ── HTTP 工具函数 ──────────────────────────────────────────────

async def _get(url: str, params: dict | None = None) -> Any:
    """带重试的 GET 请求，返回响应 JSON（dict / list 等）。"""
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for attempt in range(_MAX_RETRY):
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt < _MAX_RETRY - 1:
                    await asyncio.sleep(2 ** attempt)
                    logger.warning("[tools] GET %s 第%d次失败，重试: %s", url, attempt + 1, e)
    raise last_exc


# ── 业务查询函数 ───────────────────────────────────────────────

async def _fetch_order_list(user_id: str) -> list[dict]:
    """获取用户订单列表。"""
    url  = f"{_base_url()}/getOrderList/{user_id}"
    data = await _get(url)
    # 兼容 {code, data: [...]} 和直接返回列表两种格式
    if isinstance(data, list):
        return data
    return data.get("data") or data.get("rows") or data.get("list") or []


async def _fetch_logistics(delivery_sn: str, delivery_company: str | None = None) -> Any:
    """查询物流轨迹（原始 JSON，交由 _normalize_logistics_payload 解析）。"""
    params: dict = {"deliverySn": delivery_sn}
    if delivery_company:
        params["deliveryCompany"] = delivery_company
    return await _get(f"{_base_url()}/getLogisticsTrace", params=params)


def _append_trace_status(candidates: list[str], value: Any) -> None:
    if value is None:
        return
    t = str(value).strip()
    if not t or t.lower() in ("null", "none", "undefined", "未知"):
        return
    candidates.append(t)


def _normalize_logistics_payload(raw: Any, tracking_no: str) -> dict[str, Any]:
    """
    将 OMS 物流接口响应转为扁平、可展示结构。
    若无法解析出任何有效轨迹/状态文案，则返回 error，避免 LLM 面对「全未知」编造在途时效。
    """
    tn = str(tracking_no or "").strip()
    err: dict[str, Any] = {
        "error": "no_verified_trace",
        "message": "未查询到该运单的有效物流信息，请核对运单号或稍后再试",
        "tracking_no": tn,
    }
    candidates: list[str] = []

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                _append_trace_status(
                    candidates,
                    item.get("context")
                    or item.get("desc")
                    or item.get("remark")
                    or item.get("status"),
                )
        if not candidates:
            return err
        return {
            "tracking_no": tn,
            "status": candidates[-1],
            "current_location": "",
            "carrier": "",
            "estimated_arrival": "",
            "_verified": True,
        }

    if not isinstance(raw, dict):
        return err

    code = raw.get("code")
    if code is not None and str(code) not in ("0", "200", "20000"):
        return {
            "error": str(code),
            "message": str(raw.get("msg") or raw.get("message") or "物流查询失败"),
            "tracking_no": tn,
        }

    data = raw.get("data")
    if data in (None, "", [], {}):
        return err

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _append_trace_status(
                    candidates,
                    item.get("context")
                    or item.get("desc")
                    or item.get("remark")
                    or item.get("status"),
                )
    elif isinstance(data, dict):
        _append_trace_status(
            candidates,
            data.get("status") or data.get("logisticsStatus") or data.get("state"),
        )
        for key in ("traces", "traceList", "list", "records", "logisticsTraceList", "rows"):
            arr = data.get(key)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        _append_trace_status(
                            candidates,
                            item.get("context")
                            or item.get("desc")
                            or item.get("remark")
                            or item.get("status"),
                        )

    _append_trace_status(candidates, raw.get("status"))

    if not candidates:
        return err

    return {
        "tracking_no": tn,
        "status": candidates[-1],
        "current_location": "",
        "carrier": "",
        "estimated_arrival": "",
        "_verified": True,
    }


def _needs_logistics(slots: dict, user_input: str) -> bool:
    """判断是否需要追查物流：槽位有 tracking_no，或问句含物流关键词。"""
    if slots.get("tracking_no"):
        return True
    return any(kw in user_input for kw in _LOGISTICS_KEYWORDS)


# ── 降级结果工厂 ──────────────────────────────────────────────

def _order_fallback(slots: dict | None, reason: str) -> dict[str, Any]:
    slots = slots or {}
    return {
        "error":    reason,
        "fallback": True,
        "order_id": slots.get("order_id", ""),
        "message":  "订单系统暂时无法查询，建议稍后重试或联系人工客服",
    }


# ── 统一订单处理 ───────────────────────────────────────────────

async def _handle_order(slots: dict, user_id: str | None, user_input: str) -> dict[str, Any]:
    """
    统一订单查询入口：
    1. 查订单列表，按 order_id 过滤（无则取最新）
    2. 若用户问物流，继续查物流轨迹并合并到结果中
    """
    if not user_id:
        return _order_fallback(slots, "缺少用户ID，无法查询订单")

    orders = await _fetch_order_list(user_id)
    if not orders:
        return _order_fallback(slots, "暂无订单记录")

    # 按 order_id 匹配，未匹配则取最新
    order_id = slots.get("order_id")
    target: dict | None = None
    if order_id:
        for o in orders:
            if str(o.get("orderSn") or o.get("id") or o.get("orderId", "")) == str(order_id):
                target = o
                break
    if not target:
        target = orders[0]

    result: dict[str, Any] = dict(target)
    logger.info("[tools] 订单查询成功: orderSn=%s user_id=%s", target.get("orderSn"), user_id)

    # 判断是否需要查物流
    if _needs_logistics(slots, user_input):
        tracking_no = slots.get("tracking_no") or target.get("deliverySn") or target.get("trackingNo")
        if tracking_no:
            try:
                logistics_raw = await _fetch_logistics(tracking_no, target.get("deliveryCompany"))
                logistics = _normalize_logistics_payload(logistics_raw, str(tracking_no))
                result["logistics"] = logistics
                if logistics.get("error"):
                    logger.info("[tools] 物流无有效轨迹: deliverySn=%s", tracking_no)
                else:
                    logger.info("[tools] 物流查询有轨迹: deliverySn=%s", tracking_no)
            except Exception as e:
                logger.warning("[tools] 物流查询失败，仅返回订单信息: %s", e)
                result["logistics"] = {"error": str(e), "message": "物流信息暂时无法查询"}
        else:
            result["logistics"] = {"message": "订单中未找到物流单号，可能尚未发货"}

    return result


# ── 节点入口 ──────────────────────────────────────────────────

async def tools_node(state: CustomerServiceState) -> dict[str, Any]:
    """
    工具调用节点。
    order 意图统一查询订单，按需追查物流。

    输入:  intent, intent_slots, user_id, user_input
    输出:  order_result / tool_error
    """
    t0         = time.time()
    slots      = state.get("intent_slots") or {}
    user_id    = state.get("user_id")
    user_input = state.get("user_input", "")

    updates: dict[str, Any] = {
        "order_result": None,
        "tool_error":   None,
    }

    try:
        updates["order_result"] = await _handle_order(slots, user_id, user_input)
    except Exception as e:
        logger.error("[tools] 订单查询失败（已重试%d次）: %s", _MAX_RETRY, e, exc_info=True)
        updates["order_result"] = _order_fallback(slots, str(e))
        updates["tool_error"]   = str(e)

    debug_info = dict(state.get("debug_info") or {})
    debug_info["tools_ms"]      = round((time.time() - t0) * 1000)
    debug_info["tool_fallback"] = updates["tool_error"] is not None
    updates["debug_info"] = debug_info
    return updates
