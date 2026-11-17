"""
日志模块

基于 loguru 的统一日志配置，支持彩色控制台输出和文件滚动存储。
"""

import sys
from functools import lru_cache
from pathlib import Path

from loguru import logger as _logger


@lru_cache(maxsize=1)
def get_logger(name: str = "agent", log_dir: str = "./logs"):
    """
    获取配置好的 loguru logger 实例（单例）。

    Args:
        name:    日志文件名前缀
        log_dir: 日志目录（自动创建）

    Returns:
        loguru Logger 实例

    Example:
        >>> log = get_logger()
        >>> log.info("智能体启动")
        >>> log.debug("状态: {state}", state={"messages": []})
    """
    from agent_framework.config.settings import get_settings
    settings = get_settings()

    # 移除默认 handler
    _logger.remove()

    # 控制台输出
    _logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
               "<cyan>{name}</cyan> - <level>{message}</level>",
        colorize=True,
    )

    # 文件输出（按天滚动，保留 7 天）
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    _logger.add(
        log_path / f"{name}_{{time:YYYY-MM-DD}}.log",
        level="DEBUG",
        rotation="00:00",
        retention="7 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name} | {message}",
    )

    return _logger
