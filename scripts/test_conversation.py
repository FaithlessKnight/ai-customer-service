"""
多轮对话测试脚本

测试内容：
1. 回复内容是否符合规范（无套话、无编造、语气自然）
2. 分条是否正常（reply_parts）
3. 各节点耗时分析
4. 多轮上下文是否保持

运行方式：
    python scripts/test_conversation.py
"""
from __future__ import annotations

import json
import time
import sys
import os
import httpx

BASE_URL = "http://localhost:8000"
SESSION_ID = f"test-{int(time.time())}"

# ── 测试用例：模拟真实用户多轮对话场景 ───────────────────────────
ROUNDS = [
    # (描述, 消息)
    ("问候/模糊输入",       "你好"),
    ("产品成分咨询+皮肤需求", "你们沐浴乳有什么成分？我皮肤偏干"),
    ("追问细节",           "氨基酸配方和普通沐浴乳有什么区别？"),
    ("价格询问",           "多少钱？"),
    ("活动询问（易编造）",   "现在有什么优惠活动吗"),
    ("孕妇适用（知识库无）", "我老婆怀孕了，她能用吗"),
    ("购买意向信号",        "听起来不错，我考虑买一瓶"),
    ("投诉场景",           "上次买的包装破损了，很失望"),
    ("情绪激动（转人工）",   "你们这什么破服务！！退款！！！"),
    ("闲聊偏离（应拒绝）",  "你会写代码吗"),
]

# ── 规范检查规则 ──────────────────────────────────────────────
FORBIDDEN_PHRASES = [
    "感谢您的耐心等待", "感谢您的咨询", "本客服将竭诚",
    "非常感谢您", "很高兴为您服务", "如有其他疑问，欢迎随时",
    "满199减", "买即赠", "限时折扣", "秒杀", "立减",
    "孕妇可以放心", "敏感肌可以放心", "美白", "祛痘", "抗衰",
]

FORBIDDEN_EMOJI_RANGE = range(0x1F300, 0x1FAFF)


def check_reply(reply: str) -> list[str]:
    """检查回复是否违反规范，返回问题列表。"""
    issues = []
    for phrase in FORBIDDEN_PHRASES:
        if phrase in reply:
            issues.append(f"含禁止短语：「{phrase}」")
    for char in reply:
        if ord(char) in FORBIDDEN_EMOJI_RANGE:
            issues.append(f"含 emoji：{char}")
            break
    if len(reply) > 300:
        issues.append(f"回复过长：{len(reply)} 字符")
    return issues


def print_divider(title: str = ""):
    width = 60
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{'─' * pad} {title} {'─' * pad}")
    else:
        print("─" * width)


# ── 主测试流程 ────────────────────────────────────────────────
def main():
    print_divider("多轮对话测试")
    print(f"Session ID : {SESSION_ID}")
    print(f"Base URL   : {BASE_URL}")
    print(f"测试轮数   : {len(ROUNDS)}")

    total_ms = []
    issues_found = []

    for i, (desc, message) in enumerate(ROUNDS, 1):
        print_divider(f"第 {i} 轮：{desc}")
        print(f"用户：{message}")

        payload = {
            "message":    message,
            "session_id": SESSION_ID,
        }

        t0 = time.perf_counter()
        try:
            resp = httpx.post(
                f"{BASE_URL}/cs",
                json=payload,
                timeout=30.0,
            )
            elapsed = round((time.perf_counter() - t0) * 1000)
            total_ms.append(elapsed)

            if resp.status_code != 200:
                print(f"❌ HTTP {resp.status_code}: {resp.text}")
                continue

            data = resp.json()
        except Exception as e:
            print(f"❌ 请求失败: {e}")
            continue

        reply       = data.get("reply", "")
        parts       = data.get("reply_parts", [])
        intent      = data.get("intent", "")
        emotion     = data.get("emotion", "")
        transfer    = data.get("need_human_transfer", False)

        # 打印回复
        print(f"\n客服回复（{elapsed}ms | 意图={intent} 情绪={emotion} 转人工={transfer}）：")
        if len(parts) > 1:
            for j, part in enumerate(parts, 1):
                print(f"  [{j}] {part}")
        else:
            print(f"  {reply}")

        # 分条检查
        if len(parts) == 0:
            print("  ⚠️  reply_parts 为空")
        elif len(parts) == 1 and len(reply) > 80:
            print(f"  ⚠️  回复较长（{len(reply)}字）但未分条，考虑是否需要拆分")
        else:
            print(f"  ✓ 共 {len(parts)} 条")

        # 规范检查
        rule_issues = check_reply(reply)
        if rule_issues:
            for issue in rule_issues:
                print(f"  ✗ 规范问题：{issue}")
            issues_found.append((i, desc, rule_issues))
        else:
            print("  ✓ 规范检查通过")

        # 获取耗时详情
        try:
            debug = httpx.get(
                f"{BASE_URL}/cs/debug/{SESSION_ID}",
                timeout=5.0,
            ).json()
            di = debug.get("debug_info", {})
            parts_count = debug.get("reply_parts_count", len(parts))
            print(
                f"  耗时详情 → analysis={di.get('analysis_ms','?')}ms  "
                f"rag={di.get('rag_ms','?')}ms(skipped={di.get('rag_skipped','?')})  "
                f"prompt={di.get('prompt_builder_ms','?')}ms  "
                f"generator={di.get('generator_ms','?')}ms  "
                f"分条={parts_count}条"
            )
        except Exception:
            pass

        time.sleep(0.8)  # 模拟用户思考间隔，避免触发限流

    # ── 汇总报告 ────────────────────────────────────────────
    print_divider("测试汇总")
    print(f"总轮数       : {len(ROUNDS)}")
    if total_ms:
        print(f"平均耗时     : {round(sum(total_ms)/len(total_ms))}ms")
        print(f"最快 / 最慢  : {min(total_ms)}ms / {max(total_ms)}ms")
        slow = [(i+1, ROUNDS[i][0], t) for i, t in enumerate(total_ms) if t > 3000]
        if slow:
            print("慢请求（>3s）:")
            for idx, desc, t in slow:
                print(f"  第{idx}轮 [{desc}] {t}ms")
    if issues_found:
        print(f"\n规范问题共 {len(issues_found)} 处：")
        for idx, desc, problems in issues_found:
            print(f"  第{idx}轮 [{desc}]：{'; '.join(problems)}")
    else:
        print("\n✓ 所有轮次规范检查通过")
    print_divider()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
