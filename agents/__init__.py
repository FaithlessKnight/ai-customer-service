"""
业务层智能体目录

在此目录中创建你的业务智能体，继承 BaseAgent 或直接使用预置模板。

示例:
    from agent_framework.agents.react_agent import ReactAgent

    class CustomerServiceAgent(ReactAgent):
        def __init__(self):
            super().__init__(
                system_prompt="你是一名专业的客服助手...",
                checkpointer_backend="sqlite",
            )
"""
