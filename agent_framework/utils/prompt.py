"""
Prompt 工具

提供 Prompt 模板加载和格式化工具。
支持从文件加载或直接使用字符串模板。
"""

from pathlib import Path
from string import Template
from typing import Optional


def load_prompt(filepath: str, encoding: str = "utf-8") -> str:
    """
    从文件加载 Prompt 模板文本。

    Args:
        filepath: Prompt 文件路径（.txt 或 .md）
        encoding: 文件编码

    Returns:
        Prompt 模板字符串

    Example:
        >>> prompt = load_prompt("prompts/customer_service.txt")
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {filepath}")
    return path.read_text(encoding=encoding)


def format_prompt(template: str, **kwargs) -> str:
    """
    使用关键字参数格式化 Prompt 模板。
    支持 {variable} 和 $variable 两种占位符风格。

    Args:
        template: Prompt 模板字符串
        **kwargs: 替换变量

    Returns:
        格式化后的 Prompt 字符串

    Example:
        >>> tpl = "你是 {role}，请用 {language} 回答用户。"
        >>> format_prompt(tpl, role="客服", language="中文")
        '你是 客服，请用 中文 回答用户。'
    """
    try:
        return template.format(**kwargs)
    except KeyError:
        # 回退到 $variable 风格
        return Template(template).safe_substitute(**kwargs)
