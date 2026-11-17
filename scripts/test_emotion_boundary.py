"""
情绪分类边界测试：验证轻量负反馈不再误判为 angry，严重投诉仍正常转人工。
"""
from __future__ import annotations
import json
import time
import requests

BASE = "http://localhost:8000"
SID_PREFIX = f"emo_test_{int(time.time())}"

CASES = [
    ("L1", "你们沐浴乳洗完有点干的感觉",          "calm",  False, "原报告 case"),
    ("L2", "洗完有点紧绷",                          "calm",  False, "肤感轻负反馈"),
    ("L3", "泡沫一般般，没那么丰富",                "calm",  False, "泡沫吐槽"),
    ("L4", "味道还行吧，不是特别喜欢",              "calm",  False, "香味中性偏负"),
    ("L5", "用着感觉普通，没什么特别的",            "calm",  False, "整体平淡反馈"),
    ("L6", "效果一般，不像你们说的那么神",          "calm",  False, "效果质疑但无投诉"),

    ("M1", "贵了点，能便宜点吗",                    "calm",  False, "价格异议"),
    ("M2", "有没有更便宜的款",                      "calm",  False, "求其他价位"),

    ("H1", "用了过敏起疹子了，必须退！",            "angry", True,  "过敏投诉，应转人工"),
    ("H2", "包装破损漏了一半！",                    "angry", True,  "包装破损投诉"),
    ("H3", "你们东西根本不能用！要退款！",          "angry", True,  "强烈投诉退款"),
    ("H4", "气死了，等了一周还没发货",              "angry", True,  "情绪激动+物流"),

    ("N1", "什么时候到呀",                          "calm",  False, "物流时效追问"),
    ("N2", "我考虑下吧",                            "calm",  False, "犹豫收口"),
    ("N3", "[白眼]",                                "calm",  False, "中性表情"),
]

def run_case(case_id: str, msg: str, expect_emotion: str, expect_transfer: bool, note: str):
    sid = f"{SID_PREFIX}_{case_id}"
    payload = {
        "message": msg,
        "session_id": sid,
        "user_id": "test_emo_user",
        "scene": "trial_used",
    }
    try:
        r = requests.post(f"{BASE}/cs", json=payload, timeout=60)
        d = r.json()
    except Exception as e:
        return {"id": case_id, "msg": msg, "note": note, "error": str(e)}
    emo = d.get("emotion", "")
    transfer = bool(d.get("need_human_transfer"))
    ok_emo = (emo == expect_emotion)
    ok_tr = (transfer == expect_transfer)
    return {
        "id": case_id,
        "msg": msg,
        "note": note,
        "emotion": emo,
        "polarity": d.get("emotion_polarity"),
        "transfer": transfer,
        "transfer_reason": d.get("transfer_reason"),
        "intent": d.get("intent"),
        "expect_emotion": expect_emotion,
        "expect_transfer": expect_transfer,
        "pass_emotion": ok_emo,
        "pass_transfer": ok_tr,
        "pass": ok_emo and ok_tr,
        "reply_preview": (d.get("reply") or "")[:60],
    }

results = []
for cid, msg, ee, et, note in CASES:
    res = run_case(cid, msg, ee, et, note)
    results.append(res)
    mark = "PASS" if res.get("pass") else "FAIL"
    print(f"[{mark}] {cid} {note}")
    print(f"  msg     : {msg}")
    print(f"  emotion : {res.get('emotion')}({res.get('polarity')}) (期望 {ee})")
    print(f"  transfer: {res.get('transfer')} reason={res.get('transfer_reason')} (期望 {et})")
    print(f"  intent  : {res.get('intent')}")
    print(f"  reply   : {res.get('reply_preview')!r}")
    print()

passed = sum(1 for r in results if r.get("pass"))
print(f"=== {passed}/{len(results)} PASS ===")
fails = [r for r in results if not r.get("pass")]
if fails:
    print("失败 case 详情:")
    print(json.dumps(fails, ensure_ascii=False, indent=2))
