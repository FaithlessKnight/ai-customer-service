"""
analysis 节点 vs generator 节点 模型对比测试

测试维度：
  [A] analysis 模型对比（意图/情绪识别）
      - 模型：qwen-turbo / qwen-flash / qwen3.5-flash
      - 指标：延迟（ms）、结构化输出成功率、分类准确率
      - 方法：with_structured_output，与标注答案对比

  [G] generator 模型对比（回复生成）
      - 模型：qwen-flash / qwen3.5-flash
      - 指标：TTFT（ms）、总耗时（ms）、吞吐（tok/s）
      - 方法：流式调用，统计 TTFT + token 速度

运行方式：
    python scripts/benchmark_analysis_vs_generator.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import os
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from agent_framework.config.settings import get_settings
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from typing import Literal

s = get_settings()
if not s.effective_api_key:
    print("❌ API Key 未加载，请确认 .env 存在于项目根目录")
    sys.exit(1)
print(f"✓ API Key: {s.effective_api_key[:8]}...\n")

# ── 复用 prompts.py 中的真实 prompt ────────────────────────────
from agents.customer_service.prompts import ANALYSIS_SYSTEM, ANALYSIS_USER, SYSTEM_PROMPT_BASE
from datetime import datetime
GENERATOR_SYSTEM = SYSTEM_PROMPT_BASE.format(current_time=datetime.now().strftime("%Y-%m-%d %H:%M"))

# ── Pydantic 结构化输出 Schema（与 analysis.py 一致）───────────
class AnalysisResult(BaseModel):
    emotion:        Literal["calm", "excited", "angry"] = Field(description="情绪类型")
    emotion_score:  float = Field(ge=0.0, le=1.0)
    emotion_reason: str
    intents:        list[Literal["order", "knowledge", "chitchat", "unknown"]] = Field(
        default_factory=lambda: ["unknown"]
    )
    intent_score:   float = Field(ge=0.0, le=1.0)
    intent_reason:  str
    slots:          dict[str, Any] = Field(default_factory=dict)


# ── 模型实例缓存 ────────────────────────────────────────────────
_llm_cache: dict[tuple, Any] = {}

def get_llm(model: str, streaming: bool = False, structured: bool = False) -> Any:
    key = (model, streaming, structured)
    if key in _llm_cache:
        return _llm_cache[key]
    kwargs: dict[str, Any] = dict(
        model=model,
        api_key=s.effective_api_key,
        base_url=s.openai_base_url,
        temperature=0.0,
        max_tokens=200,
        streaming=streaming,
    )
    if "3.5" in model:
        kwargs["extra_body"] = {"enable_thinking": False}
    llm = ChatOpenAI(**kwargs)
    if structured:
        llm = llm.with_structured_output(AnalysisResult)
    _llm_cache[key] = llm
    return llm


# ═══════════════════════════════════════════════════════════════
# [A] analysis 测试用例（含标注答案）
# ═══════════════════════════════════════════════════════════════
ANALYSIS_CASES = [
    # (描述, 用户消息, 期望intents, 期望emotion)
    ("问候",           "你好",                                               ["chitchat"], "calm"),
    ("查成分",         "你们沐浴乳有什么成分",                               ["knowledge"], "calm"),
    ("查订单",         "我的订单发货了吗，单号2024120000001",                 ["order"],    "calm"),
    ("投诉包装",       "上次买的包装破损了，很失望",                         ["order"],    "angry"),
    ("愤怒退款",       "你们这什么破服务！！退款！！！",                     ["order"],    "angry"),
    ("购买意向",       "听起来不错，我考虑买一瓶",                           ["knowledge"], "calm"),
    ("孕妇适用",       "我老婆怀孕了，她能用吗",                             ["knowledge"], "calm"),
    ("闲聊偏题",       "你会写代码吗",                                       ["chitchat"], "calm"),
    ("多意图",         "我的订单到哪了？另外沐浴乳的成分是什么？",           ["order", "knowledge"], "calm"),
    ("活动询问",       "现在有什么优惠活动吗",                               ["knowledge"], "calm"),
    ("价格",           "多少钱",                                             ["knowledge"], "calm"),
    ("物流追踪",       "快递到哪了，运单号SF1234567890",                      ["order"],    "calm"),
    ("焦虑追单",       "怎么还没发货！！都等了三天了！",                     ["order"],    "excited"),
    ("成分对比",       "氨基酸配方和普通沐浴乳有什么区别",                   ["knowledge"], "calm"),
    ("槽位提取",       "我的订单2024080001商品是舒净雅香沐浴乳，发货了吗",   ["order"],    "calm"),
]

ANALYSIS_MODELS = ["deepseek-v4-flash"]
ANALYSIS_ROUNDS = 2  # 每条重复轮数（结构化输出相对稳定，2轮足够）


async def run_analysis_once(model: str, case_msg: str) -> dict[str, Any]:
    """单次 analysis 调用，返回延迟 + 结果。"""
    llm = get_llm(model, structured=True)
    prompt = ANALYSIS_USER.format(history="（无历史）", user_input=case_msg)
    t0 = time.perf_counter()
    try:
        result: AnalysisResult = await llm.ainvoke([
            SystemMessage(content=ANALYSIS_SYSTEM),
            HumanMessage(content=prompt),
        ])
        elapsed = round((time.perf_counter() - t0) * 1000)
        return {
            "ok":       True,
            "ms":       elapsed,
            "intents":  result.intents,
            "emotion":  result.emotion,
            "slots":    result.slots,
            "reason":   result.intent_reason[:30],
        }
    except Exception as e:
        elapsed = round((time.perf_counter() - t0) * 1000)
        return {"ok": False, "ms": elapsed, "error": str(e)[:60]}


# ═══════════════════════════════════════════════════════════════
# [G] generator 测试用例
# ═══════════════════════════════════════════════════════════════
GENERATOR_CASES = [
    ("成分咨询",    "你们沐浴乳有什么成分？我皮肤偏干，洗完会紧绷吗？"),
    ("投诉安抚",    "上次买的包装破损了，内容物有渗漏，非常失望，想退款"),
    ("孕妇适用",    "我老婆怀孕了，能用这款沐浴乳吗？成分安全吗？"),
    ("活动询问",    "现在下单有什么优惠吗？有没有满减或者赠品？"),
    ("成分对比",    "氨基酸配方和普通皂基沐浴乳有什么区别？哪个对干皮更好？"),
    ("购买引导",    "听起来这款沐浴乳挺适合我的，有没有小规格可以先试试？"),
    ("情绪激动",    "你们这是什么服务！！三天没发货，客服也不回！退款退款！"),
    ("闲聊拒绝",    "你会写代码吗？帮我写个 Python 脚本"),
]

GENERATOR_CONTEXT = """【知识库参考内容】
[参考1]
舒净雅香沐浴乳主要成分：水、月桂酸钾（皂基清洁）、甘油（保湿）、
月桂基羟基璜基甜菜碱（温和表活）、癸基葡糖苷（低刺激）、氢化蓖麻油（滋润）。
皂基+温和表活复配体系，泡沫丰富，兼顾清洁力与温和度。

[参考2]
适用人群：正常肌肤、干性肌肤、敏感肌肤均可使用。
未标注孕妇专项说明，建议孕期使用前咨询医生。
规格：100ml（试用装）、300ml、500ml。"""

GENERATOR_MODELS = ["deepseek-v4-flash"]
GENERATOR_ROUNDS = 3


async def run_generator_once(model: str, user_msg: str) -> dict[str, Any]:
    """单次流式生成，返回 TTFT + 总耗时 + tok/s。"""
    llm = get_llm(model, streaming=True)
    full_system = f"{GENERATOR_SYSTEM}\n\n{GENERATOR_CONTEXT}"
    messages = [
        SystemMessage(content=full_system),
        HumanMessage(content=user_msg),
    ]
    t0 = time.perf_counter()
    ttft = None
    token_count = 0
    full_text = ""
    try:
        async for chunk in llm.astream(messages):
            if chunk.content:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                token_count += 1
                full_text += chunk.content
        total = time.perf_counter() - t0
        return {
            "ok":         True,
            "ttft_ms":    round((ttft or 0) * 1000),
            "total_ms":   round(total * 1000),
            "tokens":     token_count,
            "tok_per_s":  round(token_count / total, 1) if total > 0 else 0,
            "preview":    full_text[:50].replace("\n", " "),
        }
    except Exception as e:
        return {"ok": False, "ttft_ms": 0, "total_ms": 0, "tokens": 0, "tok_per_s": 0,
                "error": str(e)[:60]}


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def sep(title: str = "", width: int = 64):
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{'─' * pad} {title} {'─' * pad}")
    else:
        print("─" * width)


async def main():
    sep("analysis 节点模型对比")
    print(f"  模型：{ANALYSIS_MODELS}")
    print(f"  用例：{len(ANALYSIS_CASES)} 条  每条重复 {ANALYSIS_ROUNDS} 轮\n")

    # 收集每个模型的分类准确率 + 延迟
    # analysis_summary[model] = {"latencies": [], "intent_ok": 0, "emotion_ok": 0, "slot_ok": 0, "total": 0}
    analysis_summary: dict[str, dict] = {
        m: {"latencies": [], "intent_ok": 0, "emotion_ok": 0, "slot_ok": 0, "total": 0}
        for m in ANALYSIS_MODELS
    }

    for desc, msg, expect_intents, expect_emotion in ANALYSIS_CASES:
        print(f"  [{desc}] \"{msg[:28]}...\"" if len(msg) > 28 else f"  [{desc}] \"{msg}\"")
        print(f"    期望: intents={expect_intents}  emotion={expect_emotion}")
        for model in ANALYSIS_MODELS:
            round_latencies = []
            last_result = None
            for _ in range(ANALYSIS_ROUNDS):
                r = await run_analysis_once(model, msg)
                round_latencies.append(r["ms"])
                if r["ok"]:
                    last_result = r
                await asyncio.sleep(0.3)

            avg_ms = round(statistics.mean(round_latencies))
            analysis_summary[model]["latencies"].append(avg_ms)
            analysis_summary[model]["total"] += 1

            if last_result and last_result["ok"]:
                got_intents = sorted(last_result["intents"])
                got_emotion = last_result["emotion"]
                got_slots   = last_result["slots"]

                intent_ok  = sorted(expect_intents) == got_intents
                emotion_ok = expect_emotion == got_emotion
                # 槽位检查：期望含 order_id 或 product_name 时验证
                slot_ok = True
                if "order" in expect_intents and "order_id" in msg:
                    slot_ok = bool(got_slots.get("order_id"))

                if intent_ok:
                    analysis_summary[model]["intent_ok"] += 1
                if emotion_ok:
                    analysis_summary[model]["emotion_ok"] += 1
                if slot_ok:
                    analysis_summary[model]["slot_ok"] += 1

                intent_mark  = "✓" if intent_ok  else "✗"
                emotion_mark = "✓" if emotion_ok else "✗"
                print(
                    f"    {model:<20} {avg_ms:>5}ms  "
                    f"意图{intent_mark}{got_intents}  "
                    f"情绪{emotion_mark}{got_emotion}  "
                    f"slots={got_slots if got_slots else '{}'}"
                )
            else:
                err = last_result.get("error", "失败") if last_result else "全部失败"
                print(f"    {model:<20} {avg_ms:>5}ms  ❌ {err}")
        print()

    sep("analysis 汇总")
    print(f"  {'模型':<20} {'平均延迟':>10} {'最快':>8} {'最慢':>8} {'意图准确率':>12} {'情绪准确率':>12}")
    print(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*8} {'-'*12} {'-'*12}")
    for model in ANALYSIS_MODELS:
        st = analysis_summary[model]
        lats = st["latencies"]
        total = st["total"]
        if lats and total:
            intent_acc  = f"{st['intent_ok']}/{total} ({round(st['intent_ok']/total*100)}%)"
            emotion_acc = f"{st['emotion_ok']}/{total} ({round(st['emotion_ok']/total*100)}%)"
            print(
                f"  {model:<20} {round(statistics.mean(lats)):>8}ms "
                f"{min(lats):>6}ms {max(lats):>6}ms "
                f"{intent_acc:>12} {emotion_acc:>12}"
            )

    # ── generator 测试 ──────────────────────────────────────
    sep("generator 节点模型对比")
    print(f"  模型：{GENERATOR_MODELS}")
    print(f"  用例：{len(GENERATOR_CASES)} 条  每条重复 {GENERATOR_ROUNDS} 轮\n")

    gen_summary: dict[str, dict] = {
        m: {"ttft": [], "total": [], "tok_per_s": []}
        for m in GENERATOR_MODELS
    }

    for desc, msg in GENERATOR_CASES:
        print(f"  [{desc}] \"{msg[:30]}...\"" if len(msg) > 30 else f"  [{desc}] \"{msg}\"")
        for model in GENERATOR_MODELS:
            round_results = []
            for _ in range(GENERATOR_ROUNDS):
                r = await run_generator_once(model, msg)
                if r["ok"]:
                    round_results.append(r)
                await asyncio.sleep(0.3)

            if round_results:
                avg_ttft    = round(statistics.mean(r["ttft_ms"]  for r in round_results))
                avg_total   = round(statistics.mean(r["total_ms"] for r in round_results))
                avg_tps     = round(statistics.mean(r["tok_per_s"] for r in round_results), 1)
                gen_summary[model]["ttft"].append(avg_ttft)
                gen_summary[model]["total"].append(avg_total)
                gen_summary[model]["tok_per_s"].append(avg_tps)
                print(
                    f"    {model:<20} TTFT={avg_ttft:>5}ms  "
                    f"总耗时={avg_total:>5}ms  {avg_tps}tok/s"
                )
                print(f"      └ 回复预览: {round_results[-1]['preview']}")
            else:
                print(f"    {model:<20} ❌ 全部失败")
        print()

    sep("generator 汇总")
    print(f"  {'模型':<20} {'平均TTFT':>10} {'平均总耗时':>12} {'平均tok/s':>12} {'最快TTFT':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*12} {'-'*12} {'-'*10}")
    for model in GENERATOR_MODELS:
        st = gen_summary[model]
        if st["ttft"]:
            print(
                f"  {model:<20} "
                f"{round(statistics.mean(st['ttft'])):>8}ms "
                f"{round(statistics.mean(st['total'])):>10}ms "
                f"{round(statistics.mean(st['tok_per_s']), 1):>10}tok/s "
                f"{min(st['ttft']):>8}ms"
            )

    sep("结论")
    print("  analysis 节点：延迟最低的模型是最优 analysis 候选")
    print("  generator 节点：TTFT 低 + tok/s 高 = 用户体感响应最快")
    sep()

    # ═══════════════════════════════════════════════════════════════
    # [C] 高并发压测（真实 system prompt）
    # ═══════════════════════════════════════════════════════════════
    sep("高并发压测（真实 prompt，非简化版）")
    print("  使用 SYSTEM_PROMPT_BASE + GENERATOR_CONTEXT，更接近线上实际负载")
    print(f"  system prompt 长度: {len(GENERATOR_SYSTEM)} 字符\n")

    CONCURRENCY_LEVELS = [10, 20, 50]
    CONCURRENT_ROUNDS  = 3   # 每档重复轮数取均值

    # 并发压测：每个请求用同一个 system prompt，user 从用例池循环取
    CONCURRENT_QUESTIONS = [msg for _, msg in GENERATOR_CASES]

    async def concurrent_single(model: str, user_msg: str) -> float:
        """单个并发请求，返回总耗时（秒）。"""
        llm = get_llm(model, streaming=False)
        full_system = f"{GENERATOR_SYSTEM}\n\n{GENERATOR_CONTEXT}"
        t0 = time.perf_counter()
        try:
            await llm.ainvoke([
                SystemMessage(content=full_system),
                HumanMessage(content=user_msg),
            ])
        except Exception as e:
            pass
        return time.perf_counter() - t0

    async def run_concurrent_batch(model: str, concurrency: int) -> dict:
        questions = (CONCURRENT_QUESTIONS * 10)[:concurrency]
        t_wall_start = time.perf_counter()
        durations = await asyncio.gather(*[concurrent_single(model, q) for q in questions])
        t_wall = time.perf_counter() - t_wall_start
        sorted_d = sorted(durations)
        return {
            "wall_ms":    round(t_wall * 1000),
            "avg_ms":     round(statistics.mean(durations) * 1000),
            "p50_ms":     round(sorted_d[int(len(sorted_d) * 0.50)] * 1000),
            "p95_ms":     round(sorted_d[int(len(sorted_d) * 0.95)] * 1000),
            "throughput": round(concurrency / t_wall, 2),
        }

    for concurrency in CONCURRENCY_LEVELS:
        print(f"  ── 并发数 {concurrency} ──")
        print(f"  {'模型':<20} {'墙上耗时':>10} {'avg':>8} {'p50':>8} {'p95':>8} {'吞吐(req/s)':>12}")
        print(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*12}")
        for model in GENERATOR_MODELS:
            round_results = []
            for rnd in range(CONCURRENT_ROUNDS):
                r = await run_concurrent_batch(model, concurrency)
                round_results.append(r)
                await asyncio.sleep(2)   # 批次间休息，避免触发限流

            avg_wall  = round(statistics.mean(r["wall_ms"]    for r in round_results))
            avg_avg   = round(statistics.mean(r["avg_ms"]     for r in round_results))
            avg_p50   = round(statistics.mean(r["p50_ms"]     for r in round_results))
            avg_p95   = round(statistics.mean(r["p95_ms"]     for r in round_results))
            avg_tput  = round(statistics.mean(r["throughput"] for r in round_results), 2)
            print(
                f"  {model:<20} {avg_wall:>8}ms "
                f"{avg_avg:>6}ms {avg_p50:>6}ms {avg_p95:>6}ms "
                f"{avg_tput:>10}req/s"
            )
        print()

    sep("说明")
    print("  wall_ms  = 整批请求完成总时间（越短整体吞吐越高）")
    print("  avg      = 单请求平均耗时（用户平均等待时间）")
    print("  p50/p95  = 中位数 / 95分位耗时（p95 越低服务越稳定）")
    print("  throughput = 每秒完成请求数（越高越好）")
    sep()


if __name__ == "__main__":
    asyncio.run(main())
