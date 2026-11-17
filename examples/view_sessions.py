"""
查看 PostgreSQL 中存储的对话历史

用法:
    # 列出所有会话
    python examples/view_sessions.py

    # 查看指定会话的完整对话
    python examples/view_sessions.py user-001-abc
"""
from __future__ import annotations

import sys
import os

# 切换到项目根目录，确保 pydantic-settings 能找到 .env
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)


def list_sessions():
    """列出 PostgreSQL 中所有会话 ID。"""
    import psycopg
    from agent_framework.config.settings import get_settings
    s = get_settings()
    with psycopg.connect(s.pg_uri) as conn:
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
        ).fetchall()

    if not rows:
        print("PostgreSQL 中暂无对话记录。")
        return

    print(f"共找到 {len(rows)} 个会话：\n")
    for row in rows:
        print(f"  {row[0]}")
    print("\n用法: python examples/view_sessions.py <session_id>")


def view_session(session_id: str):
    """打印指定会话的完整对话记录。"""
    from agents.customer_service.agent import CustomerServiceAgent
    from langchain_core.messages import HumanMessage, AIMessage

    agent    = CustomerServiceAgent()
    config   = {"configurable": {"thread_id": session_id}}

    try:
        snapshot = agent._graph.get_state(config)
    except Exception as e:
        print(f"读取会话失败: {e}")
        return

    messages = snapshot.values.get("messages", [])
    if not messages:
        print(f"会话 {session_id!r} 不存在或无消息记录。")
        return

    print(f"会话 ID: {session_id}  （共 {len(messages)} 条消息）")
    print("─" * 60)
    for msg in messages:
        if isinstance(msg, HumanMessage):
            print(f"[用户]  {msg.content}")
        elif isinstance(msg, AIMessage):
            print(f"[客服]  {msg.content}")
        else:
            print(f"[{type(msg).__name__}]  {msg.content}")
    print("─" * 60)

    state = snapshot.values
    print(f"\n最后状态:")
    print(f"  情绪: {state.get('emotion', '-')}  (score={state.get('emotion_score', '-')})")
    print(f"  意图: {state.get('intent', '-')}")
    print(f"  转人工: {state.get('need_human_transfer', False)}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        view_session(sys.argv[1])
    else:
        list_sessions()
