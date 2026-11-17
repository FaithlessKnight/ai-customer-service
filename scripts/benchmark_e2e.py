"""
端到端并发压测脚本

直接对 POST /cs 接口发请求，完整经过智能体体系：
  限流 → analysis → tools/rag → prompt_builder → generator → checkpointer

测试维度：
  [1] 单用户多轮对话：测端到端各轮次耗时 + 节点拆分（analysis/rag/generator）
  [2] 多用户并发：模拟 N 个用户同时发请求，测吞吐 + 尾延迟
  [3] 模型对比：切换 generator 模型（qwen-flash vs qwen3.5-flash），对比端到端差异

运行方式：
    python scripts/benchmark_e2e.py
    python scripts/benchmark_e2e.py --concurrency 10 20 50
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import os
import time
from typing import Any

import httpx

BASE_URL = "http://localhost:8000"
TIMEOUT  = 45.0   # 单请求超时（秒），端到端比纯 API 慢，给足余量

# ── 测试用例（覆盖各意图/情绪分支）────────────────────────────────
# (描述, 消息, user_id)
SINGLE_TURN_CASES = [
    ("问候",           "你好",                                          None),
    ("产品成分+干皮",   "你们沐浴乳有什么成分？我皮肤偏干，会紧绷吗？", None),
    ("成分对比",       "氨基酸配方和普通皂基沐浴乳有什么区别？",        None),
    ("孕妇适用",       "我老婆怀孕了，能用这款沐浴乳吗？",              None),
    ("活动询问",       "现在有什么优惠活动吗",                          None),
    ("价格",           "多少钱",                                        None),
    ("购买意向",       "听起来不错，我考虑买一瓶试试",                  None),
    ("查订单",         "我的订单发货了吗",                              "member-10086"),
    ("物流追踪",       "快递到哪了",                                    "member-10086"),
    ("投诉包装",       "上次买的包装破损了，很失望",                    None),
    ("愤怒退款",       "你们这什么破服务！！退款！！！",                None),
    ("闲聊拒绝",       "你会写代码吗",                                  None),
]

# 并发压测用的消息池（混合各意图）
CONCURRENT_MESSAGES = [
    ("你好",                                          None),
    ("你们沐浴乳有什么成分？我皮肤偏干",              None),
    ("氨基酸配方和普通沐浴乳有什么区别",              None),
    ("现在有什么优惠活动吗",                          None),
    ("我的订单发货了吗",                              "member-10086"),
    ("上次买的包装破损了，很失望",                    None),
    ("听起来不错，我考虑买一瓶",                      None),
    ("你们这什么破服务！！退款！！！",                None),
]


def sep(title: str = "", width: int = 66):
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{'─' * pad} {title} {'─' * pad}")
    else:
        print("─" * width)


# ── 单次请求 ───────────────────────────────────────────────────────
async def single_request(
    client: httpx.AsyncClient,
    message: str,
    session_id: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message, "session_id": session_id}
    if user_id:
        payload["user_id"] = user_id

    t0 = time.perf_counter()
    try:
        resp = await client.post(f"{BASE_URL}/cs", json=payload, timeout=TIMEOUT)
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "ms": elapsed_ms, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        return {
            "ok":       True,
            "ms":       elapsed_ms,
            "intent":   data.get("intent", ""),
            "emotion":  data.get("emotion", ""),
            "transfer": data.get("need_human_transfer", False),
            "parts":    len(data.get("reply_parts") or []),
            "reply":    (data.get("reply") or "")[:60].replace("\n", " "),
        }
    except Exception as e:
        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        return {"ok": False, "ms": elapsed_ms, "error": str(e)[:60]}


async def get_debug(client: httpx.AsyncClient, session_id: str) -> dict:
    try:
        resp = await client.get(f"{BASE_URL}/cs/debug/{session_id}", timeout=5.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════════
# [1] 单用户串行：完整节点耗时拆分
# ═══════════════════════════════════════════════════════════════════
async def run_serial_benchmark():
    sep("单用户串行测试（完整节点耗时拆分）")
    print(f"  共 {len(SINGLE_TURN_CASES)} 条用例，每条独立 session\n")
    print(f"  {'用例':<14} {'总耗时':>8} {'意图':>10} {'情绪':>8}  analysis  rag(skip)  generator  分条")
    print(f"  {'-'*14} {'-'*8} {'-'*10} {'-'*8}  {'-'*8}  {'-'*9}  {'-'*9}  {'-'*4}")

    all_ms: list[int] = []
    analysis_ms_list: list[int] = []
    generator_ms_list: list[int] = []

    async with httpx.AsyncClient() as client:
        for desc, msg, uid in SINGLE_TURN_CASES:
            sid = f"bench-serial-{int(time.time() * 1000)}"
            r = await single_request(client, msg, sid, uid)
            if not r["ok"]:
                print(f"  {desc:<14} ❌ {r.get('error', '失败')}")
                continue

            all_ms.append(r["ms"])

            # 拉取节点耗时
            dbg = await get_debug(client, sid)
            di  = dbg.get("debug_info") or {}
            a_ms  = di.get("analysis_ms",  "?")
            r_ms  = di.get("rag_ms",        "?")
            skip  = di.get("rag_skipped",   "?")
            g_ms  = di.get("generator_ms",  "?")
            parts = dbg.get("reply_parts_count", r["parts"])

            if isinstance(a_ms, int):
                analysis_ms_list.append(a_ms)
            if isinstance(g_ms, int):
                generator_ms_list.append(g_ms)

            transfer_mark = " ⚑转人工" if r["transfer"] else ""
            print(
                f"  {desc:<14} {r['ms']:>6}ms  {r['intent']:>10} {r['emotion']:>8}{transfer_mark}"
                f"  {str(a_ms):>6}ms  {str(r_ms):>4}ms({'Y' if skip else 'N':>1})  "
                f"{str(g_ms):>6}ms  {parts}条"
            )
            await asyncio.sleep(0.5)

    sep("串行汇总")
    if all_ms:
        print(f"  总耗时  均值={round(statistics.mean(all_ms))}ms  "
              f"最快={min(all_ms)}ms  最慢={max(all_ms)}ms  "
              f"p95={sorted(all_ms)[int(len(all_ms)*0.95)]}ms")
    if analysis_ms_list:
        print(f"  analysis 均值={round(statistics.mean(analysis_ms_list))}ms  "
              f"最快={min(analysis_ms_list)}ms  最慢={max(analysis_ms_list)}ms")
    if generator_ms_list:
        print(f"  generator 均值={round(statistics.mean(generator_ms_list))}ms  "
              f"最快={min(generator_ms_list)}ms  最慢={max(generator_ms_list)}ms")
    other_ms = [
        all_ms[i] - (analysis_ms_list[i] if i < len(analysis_ms_list) else 0)
                  - (generator_ms_list[i] if i < len(generator_ms_list) else 0)
        for i in range(min(len(all_ms), len(analysis_ms_list), len(generator_ms_list)))
    ]
    if other_ms:
        print(f"  其余开销（RAG/PG/ES/路由）均值={round(statistics.mean(other_ms))}ms")


# ═══════════════════════════════════════════════════════════════════
# [2] 多用户并发
# ═══════════════════════════════════════════════════════════════════
async def run_concurrent_benchmark(concurrency_levels: list[int]):
    sep("多用户并发压测")
    print(f"  每档 2 轮取均值，每轮使用时间戳唯一 session_id（每请求都经历完整 PG 初始化）\n")

    ROUNDS = 2

    async def one_user(
        client: httpx.AsyncClient,
        user_idx: int,
        msg: str,
        uid: str | None,
        batch_ts: int,
    ) -> float:
        # 每批次用唯一时间戳前缀，确保每个请求都是全新 session，
        # 完整经历 PG 初始化 + analysis + tools/rag + generator，避免缓存穿透
        sid = f"bench-{batch_ts}-u{user_idx}"
        t0  = time.perf_counter()
        await single_request(client, msg, sid, uid)
        return time.perf_counter() - t0

    print(f"  {'模型配置':<16} {'并发':>6}  {'wall':>8}  {'avg':>8}  {'p50':>8}  {'p95':>8}  {'吞吐':>10}")
    print(f"  {'-'*16} {'-'*6}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*10}")

    for concurrency in concurrency_levels:
        round_results = []
        async with httpx.AsyncClient() as client:
            for rnd in range(ROUNDS):
                pool = (CONCURRENT_MESSAGES * 10)[:concurrency]
                batch_ts = int(time.time() * 1000)
                t_wall = time.perf_counter()
                durations = await asyncio.gather(*[
                    one_user(client, i, msg, uid, batch_ts)
                    for i, (msg, uid) in enumerate(pool)
                ])
                wall = time.perf_counter() - t_wall
                sd = sorted(durations)
                round_results.append({
                    "wall_ms":    round(wall * 1000),
                    "avg_ms":     round(statistics.mean(durations) * 1000),
                    "p50_ms":     round(sd[int(len(sd) * 0.50)] * 1000),
                    "p95_ms":     round(sd[int(len(sd) * 0.95)] * 1000),
                    "throughput": round(concurrency / wall, 2),
                })
                await asyncio.sleep(5)   # 批次间休息，让 LLM API 速率窗口重置

        aw  = round(statistics.mean(r["wall_ms"]    for r in round_results))
        aa  = round(statistics.mean(r["avg_ms"]     for r in round_results))
        a50 = round(statistics.mean(r["p50_ms"]     for r in round_results))
        a95 = round(statistics.mean(r["p95_ms"]     for r in round_results))
        at  = round(statistics.mean(r["throughput"] for r in round_results), 2)
        print(f"  {'当前配置':<16} {concurrency:>6}  {aw:>6}ms  {aa:>6}ms  {a50:>6}ms  {a95:>6}ms  {at:>8}req/s")
        print(f"    各轮: " + "  ".join(
            f"wall={r['wall_ms']}ms p95={r['p95_ms']}ms" for r in round_results
        ))


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════
async def main(concurrency_levels: list[int]):
    # 健康检查
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/health", timeout=5.0)
            info = resp.json()
            print(f"[OK] 服务健康: {info}")
    except Exception as e:
        print(f"[ERROR] 服务不可达: {e}，请先启动后端")
        return

    await run_serial_benchmark()
    await run_concurrent_benchmark(concurrency_levels)

    sep("说明")
    print("  wall     = 整批请求全部完成的总时间")
    print("  avg/p50  = 单请求平均耗时 / 中位耗时（用户典型等待时间）")
    print("  p95      = 95% 请求在此时间内完成（尾延迟，越低越稳定）")
    print("  吞吐     = 每秒完成的请求数")
    print("  其余开销 = 总耗时 - analysis - generator（含RAG/PG/ES/路由）")
    sep()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="端到端并发压测")
    parser.add_argument(
        "--concurrency", nargs="+", type=int,
        default=[10, 20, 50],
        help="并发级别列表，默认 10 20 50",
    )
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    asyncio.run(main(args.concurrency))
