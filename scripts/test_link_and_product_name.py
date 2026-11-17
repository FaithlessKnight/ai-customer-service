"""
专项回归测试：
  1. 小程序链接频控 —— 同一会话中链接最多发 1 次（首次咨询触达），
     除非客户后续明确表达"想买/下单/要链接"。
  2. 产品全称使用 —— 提到产品时优先使用「新肌秀舒净雅香沐浴乳」全称，
     不允许出现"新肌秀的沐浴乳"、"某款沐浴乳"等擅自简化的称呼。

运行方式：
    python scripts/test_link_and_product_name.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import time

# Windows 控制台默认 GBK 编码，强制 stdout 使用 utf-8 否则 ✓/✗ 会崩
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import httpx

BASE_URL = "http://localhost:8000"
SESSION_ID = f"link-name-{int(time.time())}"

# 产品全称 / 允许的简称 / 不允许的简化
PRODUCT_FULL_NAME = "新肌秀舒净雅香沐浴乳"
ALLOWED_REFERENCES = ["这款产品", "这款沐浴乳", "这款", "新肌秀舒净雅香沐浴乳"]
DISALLOWED_REFERENCES = [
    "新肌秀的沐浴乳",
    "新肌秀沐浴乳",  # 缺"舒净雅香"
    "某款沐浴乳",
    "我们的沐浴乳",  # 太宽泛，运营禁止
    "咱们的沐浴乳",
]

LINK_PATTERN = re.compile(r"#小程序://\S+")

# 对话流 A：从咨询到明确购买
ROUNDS_A: list[tuple[str, str, dict]] = [
    ("问候", "你好", {
        "expect_link": False,
        "reason": "纯问候，无购买意向，禁止发链接",
    }),
    ("产品咨询", "这款沐浴乳清洁力怎么样？泡沫多不多", {
        "expect_link": "maybe",
        "reason": "首次产品咨询，可发链接（最多1次）",
    }),
    ("追问香味", "香味是怎么样的，会不会很冲", {
        "expect_link": False,
        "reason": "继续追问产品细节，未表达购买意向，禁止再发链接",
    }),
    ("追问规格", "有多大瓶？", {
        "expect_link": False,
        "reason": "问规格仍属咨询，禁止再发链接",
    }),
    ("明确购买意向", "我决定买了，给我个购买链接", {
        "expect_link": True,
        "reason": "明确表达购买/要链接，必须发链接",
    }),
    ("付款后追问", "下单了，多久能发货", {
        "expect_link": False,
        "reason": "已下单转售后咨询，不应再发链接",
    }),
    ("感谢收尾", "谢谢", {
        "expect_link": False,
        "reason": "客户收尾，不应再发链接",
    }),
]

# 对话流 B：诱导犯错 — 模糊好评 + 直接问产品名
ROUNDS_B: list[tuple[str, str, dict]] = [
    ("询问产品", "你们卖的是什么产品", {
        "expect_link": False,
        "reason": "客户问产品身份，应说出标准名，不需发链接",
        "expect_full_name": True,  # 此轮必须出现全称
    }),
    ("成分追问", "成分安全吗", {
        "expect_link": False,
        "reason": "继续问产品细节",
    }),
    ("模糊好评1", "嗯，听起来还可以", {
        "expect_link": False,
        "reason": "模糊好评不等于购买意向，不应发链接",
    }),
    ("模糊好评2", "感觉挺不错的", {
        "expect_link": False,
        "reason": "继续模糊好评，仍不应发链接",
    }),
    ("再问价格", "多少钱一瓶", {
        "expect_link": False,
        "reason": "问价格属咨询，价格异议未澄清前不应直接发链接",
    }),
    ("活动询问", "现在有什么优惠", {
        "expect_link": False,
        "reason": "问优惠属咨询，不应直接发链接",
    }),
    ("明确下单", "好的，我要买，链接发我", {
        "expect_link": True,
        "reason": "明确购买，必须发链接",
    }),
    ("再次连发", "再给我发一次链接", {
        "expect_link": True,
        "reason": "客户主动二次索要，可以发",
    }),
]


def chat(message: str, session_id: str) -> dict:
    payload = {"message": message, "session_id": session_id, "user_id": "test-uid-001"}
    resp = httpx.post(f"{BASE_URL}/cs", json=payload, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def find_disallowed_names(text: str) -> list[str]:
    """返回出现的不规范产品称呼。"""
    found = []
    for bad in DISALLOWED_REFERENCES:
        if bad in text:
            # "新肌秀沐浴乳" 是 "新肌秀舒净雅香沐浴乳" 的子串，需排除
            if bad == "新肌秀沐浴乳":
                # 从原文中剔除全称后再判断
                stripped = text.replace(PRODUCT_FULL_NAME, "")
                if bad in stripped:
                    found.append(bad)
            else:
                found.append(bad)
    return found


def run_dialog(label: str, rounds: list[tuple[str, str, dict]], session_id: str,
               link_upper_bound: int) -> tuple[int, list[str]]:
    """跑一段对话，返回 (链接总次数, issue 列表)。"""
    print(f"\n========== 对话 [{label}] ==========")
    print(f"Session : {session_id}\n")

    link_count = 0
    issues: list[str] = []

    for idx, (rlabel, msg, expect) in enumerate(rounds, 1):
        print(f"── 第{idx}轮 [{rlabel}] ──")
        print(f"用户：{msg}")
        try:
            data = chat(msg, session_id)
        except Exception as e:
            print(f"❌ 请求失败：{e}")
            return link_count, issues + [f"第{idx}轮 请求失败：{e}"]

        reply = data.get("reply", "")
        parts = data.get("reply_parts", []) or [reply]
        for i, p in enumerate(parts, 1):
            print(f"  [{i}] {p}")

        # 1. 链接检查
        links = LINK_PATTERN.findall(reply)
        n_link = len(links)
        link_count += n_link

        if expect["expect_link"] is True and n_link == 0:
            issues.append(f"[{label}] 第{idx}轮[{rlabel}] 期望发链接但未发（{expect['reason']}）")
            print(f"  ✗ 链接：未发（期望发）")
        elif expect["expect_link"] is False and n_link > 0:
            issues.append(f"[{label}] 第{idx}轮[{rlabel}] 不该发链接但发了 {n_link} 次（{expect['reason']}）")
            print(f"  ✗ 链接：发了 {n_link} 次（不该发）")
        elif n_link > 1:
            issues.append(f"[{label}] 第{idx}轮[{rlabel}] 单轮发了 {n_link} 个链接（最多1个）")
            print(f"  ✗ 链接：单轮 {n_link} 个（>1）")
        else:
            print(f"  ✓ 链接：{n_link} 次（符合预期）")

        # 2. 不规范产品名
        bad_names = find_disallowed_names(reply)
        if bad_names:
            issues.append(f"[{label}] 第{idx}轮[{rlabel}] 不规范产品称呼：{bad_names}")
            print(f"  ✗ 产品名：{bad_names}")
        else:
            mentioned = any(ref in reply for ref in ALLOWED_REFERENCES)
            if mentioned:
                if PRODUCT_FULL_NAME in reply:
                    print(f"  ✓ 产品名：使用了全称")
                else:
                    print(f"  ✓ 产品名：使用了允许的指代（这款/这款产品/这款沐浴乳）")
            else:
                print(f"  · 产品名：本轮未提及")

        # 3. expect_full_name 强制要求
        if expect.get("expect_full_name") and PRODUCT_FULL_NAME not in reply:
            issues.append(f"[{label}] 第{idx}轮[{rlabel}] 应输出全称{PRODUCT_FULL_NAME}但未出现")
            print(f"  ✗ 产品名：未输出全称（本轮要求出现全称）")

        print()
        time.sleep(0.6)

    print(f"-> [{label}] 链接发送总计：{link_count} 次（合理上限 {link_upper_bound} 次）")
    if link_count > link_upper_bound:
        issues.append(f"[{label}] 全会话链接 {link_count} 次（>{link_upper_bound}）")
    return link_count, issues


def main() -> int:
    print(f"\n=== 链接频控 + 产品全称 回归测试 ===")
    print(f"Base    : {BASE_URL}")

    all_issues: list[str] = []
    sid_a = f"link-name-A-{int(time.time())}"
    sid_b = f"link-name-B-{int(time.time())}"

    # 对话A：链接最多 2 次（首次触达 + 明确购买）
    _, issues_a = run_dialog("A:咨询→购买", ROUNDS_A, sid_a, link_upper_bound=2)
    all_issues.extend(issues_a)

    # 对话B：链接最多 2 次（明确下单 + 二次索要）
    _, issues_b = run_dialog("B:诱导犯错", ROUNDS_B, sid_b, link_upper_bound=2)
    all_issues.extend(issues_b)

    print("\n" + "=" * 60)
    if all_issues:
        print(f"❌ 共发现 {len(all_issues)} 个问题：")
        for it in all_issues:
            print(f"  · {it}")
        return 1
    print("✓ 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
