"""
LangGraph 智能体框架

快速开始:
    from agent_framework import create_agent, AgentConfig
    from agent_framework.tools import web_search, calculator
"""

from agent_framework.config.settings import Settings, get_settings
from agent_framework.graph.builder import GraphBuilder
from agent_framework.agents.base import BaseAgent

__version__ = "0.1.0"
__all__ = [
    "Settings",
    "get_settings",
    "GraphBuilder",
    "BaseAgent",
]
