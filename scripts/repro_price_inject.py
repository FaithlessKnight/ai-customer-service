"""复现 trial_used + 优惠类问句的 prompt 注入，验证价格块是否进入上下文。"""
from __future__ import annotations
import asyncio, sys, io, os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from agents.customer_service.product_cache import refresh_cache, search_products
from agents.customer_service.nodes.prompt_builder import prompt_builder_node


async def main():
    await refresh_cache()
    state = {
        "user_input": "你们沐浴乳有优惠吗",
        "messages": [],
        "session_id": "debug_repro",
        "user_id": "test_id_019",
        "intent": "knowledge",
        "intents": ["knowledge"],
        "emotion": "calm",
        "emotion_polarity": "neutral",
        "scene": "trial_used",
        "scene_source": "crm",
        "retrieved_chunks": [],
        "intent_slots": {},
        "accumulated_slots": {},
    }
    out = await prompt_builder_node(state)
    ctx = out.get("context_text", "")
    print("=== context_text 长度:", len(ctx))
    print("=== 是否包含【商品价格信息】:", "商品价格信息" in ctx)
    print("=== 是否包含 '79.9':", "79.9" in ctx)
    print("=== 是否包含 '78':", "78" in ctx)
    print()
    print("--- context_text 全文 ---")
    print(ctx)


asyncio.run(main())
