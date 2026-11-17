"""
重试工具

基于 tenacity 提供指数退避重试装饰器，
适用于 LLM 调用、外部 API 请求等可能临时失败的操作。
"""

import functools
from typing import Callable, Optional, Tuple, Type


def retry_with_backoff(
    max_attempts: int = 3,
    wait_min: float = 1.0,
    wait_max: float = 10.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    reraise: bool = True,
):
    """
    指数退避重试装饰器工厂。

    Args:
        max_attempts: 最大重试次数（含首次）
        wait_min:     最小等待秒数
        wait_max:     最大等待秒数
        exceptions:   触发重试的异常类型元组
        reraise:      超出重试次数后是否重新抛出异常

    Returns:
        装饰器函数

    Example:
        >>> @retry_with_backoff(max_attempts=3)
        ... def call_llm(prompt):
        ...     return llm.invoke(prompt)
    """
    try:
        from tenacity import (
            retry,
            retry_if_exception_type,
            stop_after_attempt,
            wait_exponential,
        )

        def decorator(func: Callable) -> Callable:
            @retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=1, min=wait_min, max=wait_max),
                retry=retry_if_exception_type(exceptions),
                reraise=reraise,
            )
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper

        return decorator

    except ImportError:
        # tenacity 未安装时降级为无重试
        def decorator(func: Callable) -> Callable:
            return func
        return decorator
