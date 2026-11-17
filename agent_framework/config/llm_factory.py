"""
LLM 工厂模块

统一创建和管理不同提供商的 LLM 实例。
支持 OpenAI、Anthropic 等，轻松扩展。
"""

from typing import Literal, Optional

from langchain_core.language_models import BaseChatModel

from agent_framework.config.settings import get_settings

ProviderType = Literal["openai", "anthropic", "ollama"]


def create_llm(
    provider: ProviderType = "openai",
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    streaming: bool = False,
    **kwargs,
) -> BaseChatModel:
    """
    工厂函数：创建指定提供商的 LLM 实例。

    Args:
        provider: LLM 提供商，支持 'openai' | 'anthropic' | 'ollama'
        model: 模型名称（为空时使用配置文件默认值）
        temperature: 温度参数（为空时使用配置文件默认值）
        max_tokens: 最大 token 数（为空时使用配置文件默认值）
        streaming: 是否启用流式输出
        **kwargs: 传递给底层 LLM 的额外参数

    Returns:
        BaseChatModel 实例

    Example:
        >>> llm = create_llm(provider="openai", model="gpt-4o")
        >>> llm_stream = create_llm(streaming=True)
    """
    settings = get_settings()

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model or settings.openai_model,
            temperature=temperature if temperature is not None else settings.openai_temperature,
            max_tokens=max_tokens or settings.openai_max_tokens,
            api_key=settings.effective_api_key,
            base_url=settings.openai_base_url,
            streaming=streaming,
            **kwargs,
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model or settings.anthropic_model,
            temperature=temperature if temperature is not None else settings.openai_temperature,
            max_tokens=max_tokens or settings.openai_max_tokens,
            api_key=settings.anthropic_api_key,
            streaming=streaming,
            **kwargs,
        )

    elif provider == "ollama":
        from langchain_community.chat_models import ChatOllama

        return ChatOllama(
            model=model or "llama3.2",
            temperature=temperature if temperature is not None else settings.openai_temperature,
            **kwargs,
        )

    else:
        raise ValueError(f"不支持的 LLM 提供商: {provider}。支持: openai, anthropic, ollama")


def create_default_llm(**kwargs) -> BaseChatModel:
    """使用默认配置创建 LLM（快捷方式）。"""
    return create_llm(provider="openai", **kwargs)
