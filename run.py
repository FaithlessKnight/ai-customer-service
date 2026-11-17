"""
Windows 兼容启动脚本。
必须在 import 任何 asyncio 相关模块之前设置事件循环策略。
"""
import sys

if sys.platform == "win32":
    import asyncio
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print("[启动] 已设置 WindowsSelectorEventLoopPolicy")

import uvicorn
from agent_framework.config.settings import get_settings

s = get_settings()
uvicorn.run(
    "api.main:app",
    host=s.api_host,
    port=s.api_port,
    reload=s.is_development,
    log_level=s.log_level.lower(),
)
