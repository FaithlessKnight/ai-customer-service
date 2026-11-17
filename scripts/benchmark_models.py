"""
模型性能与简单质量对比：deepseek-v4-flash（generator 默认）vs deepseek-v4-flash（analysis 默认）

速度指标：TTFT、总耗时、流式 chunk 近似吞吐；可选并发压测。
质量指标：预设客服场景关键词命中率（侧向参考，非严格裁判）。

运行：
    python scripts/benchmark_models.py
    python scripts/benchmark_models.py --quick
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from agent_framework.config.settings import get_settings

s = get_settings()

if not s.effective_api_key:
    print("❌ DashScope / OPENAI_API_KEY 未加载，请确认 .env")
    sys.exit(1)
print(f"✓ DashScope Key: {s.effective_api_key[:8]}...")


@dataclass(frozen=True)
class ModelBenchConfig:
    label: str
    model: str
    base_url: str
    api_key: str
    extra_body: dict[str, Any] | None = None


def build_model_configs() -> list[ModelBenchConfig]:
    return [
        ModelBenchConfig(
            label="deepseek-v4-flash(当前generator默认)",
            model="deepseek-v4-flash",
            base_url=s.openai_base_url,
            api_key=s.effective_api_key,
            extra_body={"enable_thinking": False},
        ),
        ModelBenchConfig(
            label="deepseek-v4-flash(当前analysis默认)",
            model="deepseek-v4-flash",
            base_url=s.openai_base_url,
            api_key=s.effective_api_key,
            extra_body={},
        ),
    ]


MODEL_CONFIGS = build_model_configs()

_llm_instances: dict[tuple[str, bool], ChatOpenAI] = {}


def get_llm(cfg: ModelBenchConfig, streaming: bool) -> ChatOpenAI:
    key = (cfg.label, streaming)
    if key not in _llm_instances:
        kw: dict[str, Any] = {
            "model": cfg.model,
            "api_key": cfg.api_key,
            "base_url": cfg.base_url,
            "temperature": 0.3,
            "max_tokens": 200,
            "streaming": streaming,
        }
        if cfg.extra_body:
            kw["extra_body"] = cfg.extra_body
        _llm_instances[key] = ChatOpenAI(**kw)
    return _llm_instances[key]


TEST_CASES = [
    "你好，我想了解一下你们沐浴乳的主要成分，我皮肤偏干，想知道是否有保湿效果？",
    "我在2024年12月下了一笔订单，订单编号是2024120000001，请问现在发货了吗？物流在哪个环节？",
    "请问你们的沐浴乳是否通过了相关部门的质量备案？产品适用人群有哪些限制？",
    "我购买的产品到货后发现外包装有明显破损，内容物疑似受到污染，请问应该如何申请售后处理？",
    "目前500ml规格的零售价是多少？平台上是否有正在进行的促销活动或优惠券可以使用？",
    "我想批量采购贵司产品用于企业员工福利，请问是否支持企业大客户定制或团购优惠？",
    "使用沐浴乳后皮肤出现轻微泛红，请问这是正常反应还是过敏反应？需要停用吗？",
    "产品宣传中提到含有氨基酸成分，请问具体是哪种氨基酸？浓度是否达到有效护肤标准？",
]

SYSTEM_PROMPT = (
    "你是一位专业的电商客服助手，帮助顾客解决购物中遇到的问题，回复简洁温和，2~4句话。"
)

# (用例索引或原文片段, 期望覆盖的关键词，命中比例作质量侧写)
KEYWORD_SCENARIOS: list[tuple[int, list[str]]] = [
    (0, ["成分", "保湿", "干", "滋润"]),
    (1, ["订单", "物流", "发货"]),
    (4, ["价格", "元", "促销", "优惠", "活动"]),
]

ROUNDS = 3


def apply_quick_argv() -> None:
    """python scripts/benchmark_models.py --quick  少轮次、少用例，便于冒烟。"""
    global ROUNDS, TEST_CASES, KEYWORD_SCENARIOS
    if "--quick" not in sys.argv:
        return
    sys.argv.remove("--quick")
    ROUNDS = 1
    TEST_CASES = TEST_CASES[:3]
    KEYWORD_SCENARIOS = [
        (0, ["成分", "保湿", "干", "滋润"]),
        (1, ["订单", "物流", "发货"]),
        (2, ["备案", "人群", "适用"]),
    ]


def keyword_score(text: str, kws: list[str]) -> float:
    if not kws:
        return 0.0
    hits = sum(1 for k in kws if k in text)
    return hits / len(kws)


async def test_once_streaming(cfg: ModelBenchConfig, question: str) -> dict[str, Any]:
    llm = get_llm(cfg, streaming=True)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=question),
    ]
    t_start = time.perf_counter()
    ttft: float | None = None
    chunks = 0
    full_text = ""
    try:
        async for chunk in llm.astream(messages):
            if chunk.content:
                if ttft is None:
                    ttft = time.perf_counter() - t_start
                chunks += 1
                full_text += str(chunk.content)
        t_total = time.perf_counter() - t_start
    except Exception as e:
        return {"error": str(e), "model": cfg.label, "question": question}

    return {
        "model": cfg.label,
        "question": question[:24] + "...",
        "ttft_ms": round((ttft or 0) * 1000),
        "total_ms": round(t_total * 1000),
        "chunks": chunks,
        "chunks_per_s": round(chunks / t_total, 1) if t_total > 0 else 0,
        "reply_preview": full_text[:80].replace("\n", " "),
    }


async def test_concurrent(cfg: ModelBenchConfig, concurrency: int = 5) -> dict[str, Any]:
    llm = get_llm(cfg, streaming=False)

    async def single(q: str) -> float:
        t0 = time.perf_counter()
        try:
            await llm.ainvoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=q),
                ]
            )
        except Exception:
            pass
        return time.perf_counter() - t0

    questions = (TEST_CASES * 10)[:concurrency]
    t_start = time.perf_counter()
    durations = await asyncio.gather(*[single(q) for q in questions])
    t_wall = time.perf_counter() - t_start
    return {
        "model": cfg.label,
        "concurrency": concurrency,
        "wall_time_ms": round(t_wall * 1000),
        "avg_ms": round(statistics.mean(durations) * 1000),
        "p95_ms": round(sorted(durations)[int(len(durations) * 0.95)] * 1000),
        "throughput": round(concurrency / t_wall, 2),
    }


async def run_quality_sample(cfg: ModelBenchConfig) -> dict[str, Any]:
    """各场景 1 次非流式完整回复 + 关键词命中率。"""
    llm = get_llm(cfg, streaming=False)
    scores: list[float] = []
    snippets: list[str] = []
    for idx, kws in KEYWORD_SCENARIOS:
        q = TEST_CASES[idx]
        try:
            msg = await llm.ainvoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=q),
                ]
            )
            text = (msg.content or "").strip()
        except Exception as e:
            return {"model": cfg.label, "error": str(e)}
        scores.append(keyword_score(text, kws))
        snippets.append(text[:120].replace("\n", " "))
    return {
        "model": cfg.label,
        "keyword_hit_mean": round(statistics.mean(scores), 3),
        "snippets": snippets,
    }


async def main() -> None:
    print("=" * 60)
    print("模型对比：qwen3.5-flash vs qwen-flash")
    print("=" * 60)

    print("\n【1】逐条用例流式（每条重复 %d 轮均值）\n" % ROUNDS)
    results: dict[str, list[dict[str, Any]]] = {c.label: [] for c in MODEL_CONFIGS}

    for cfg in MODEL_CONFIGS:
        print(f"  → {cfg.label}")
        for case in TEST_CASES:
            round_results: list[dict[str, Any]] = []
            errors: list[str] = []
            for _ in range(ROUNDS):
                r = await test_once_streaming(cfg, case)
                if "error" not in r:
                    round_results.append(r)
                else:
                    errors.append(r["error"])
                await asyncio.sleep(0.4)
            if round_results:
                avg = {
                    "question": round_results[0]["question"],
                    "ttft_ms": round(statistics.mean(x["ttft_ms"] for x in round_results)),
                    "total_ms": round(statistics.mean(x["total_ms"] for x in round_results)),
                    "chunks_per_s": round(statistics.mean(x["chunks_per_s"] for x in round_results), 1),
                }
                results[cfg.label].append(avg)
                print(
                    f"    [{avg['question']}] TTFT={avg['ttft_ms']}ms "
                    f"总耗时={avg['total_ms']}ms {avg['chunks_per_s']}chunk/s"
                )
            else:
                print(f"    [{case[:20]}...] 失败: {errors[0] if errors else '未知'}")

    print("\n【汇总】平均延迟 / 流式 chunk 吞吐\n")
    print(f"  {'模型':<38} {'TTFT(ms)':<12} {'总耗时(ms)':<14} {'chunk/s'}")
    print("  " + "-" * 72)
    for cfg in MODEL_CONFIGS:
        rs = results.get(cfg.label, [])
        if rs:
            print(
                f"  {cfg.label:<38} "
                f"{round(statistics.mean(r['ttft_ms'] for r in rs)):<12} "
                f"{round(statistics.mean(r['total_ms'] for r in rs)):<14} "
                f"{round(statistics.mean(r['chunks_per_s'] for r in rs), 1)}"
            )

    print("\n【2】简单质量侧写（3 个场景关键词命中率，越高越贴题，仅供参考）\n")
    for cfg in MODEL_CONFIGS:
        q = await run_quality_sample(cfg)
        if "error" in q:
            print(f"  {cfg.label}: 失败 — {q['error']}")
            continue
        print(f"  {cfg.label}: 关键词平均命中率={q['keyword_hit_mean']}")
        for i, snip in enumerate(q["snippets"], 1):
            print(f"    场景{i}: {snip}...")

    print("\n【3】并发压测（10 / 20 / 50，每档 3 轮均值）\n")
    conc_levels = [10] if ROUNDS < 2 and len(TEST_CASES) <= 3 else [10, 20, 50]
    for concurrency in conc_levels:
        print(f"  并发 {concurrency}")
        for cfg in MODEL_CONFIGS:
            round_results: list[dict[str, Any]] = []
            for _ in range(3):
                r = await test_concurrent(cfg, concurrency=concurrency)
                round_results.append(r)
                await asyncio.sleep(0.8)
            avg_wall = round(statistics.mean(r["wall_time_ms"] for r in round_results))
            avg_avg = round(statistics.mean(r["avg_ms"] for r in round_results))
            avg_p95 = round(statistics.mean(r["p95_ms"] for r in round_results))
            avg_tput = round(statistics.mean(r["throughput"] for r in round_results), 2)
            print(
                f"    {cfg.label:<38} 墙时={avg_wall}ms avg={avg_avg}ms "
                f"p95={avg_p95}ms {avg_tput}req/s"
            )
        print()

    print("=" * 60)
    print("完成。说明：chunk/s 取决于供应商流式分片粒度，仅作横向参考。")
    print("=" * 60)


if __name__ == "__main__":
    apply_quick_argv()
    if ROUNDS < 2 and len(TEST_CASES) <= 3:
        print("（--quick 模式：指标仅供冒烟，正式对比请去掉 --quick）\n")
    asyncio.run(main())
