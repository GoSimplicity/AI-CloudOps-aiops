"""额外显式选择的模型验收，仅暴露本机计算器，不暴露基础设施工具"""

import os

import pytest

from app.mcp.mcp_client import MCPAssistant
from app.mcp.server.tools.calculator_tool import CalculatorTool

pytestmark = [pytest.mark.external, pytest.mark.model]


async def test_real_model_selects_calculator_and_uses_actual_result():
    if not os.getenv("LLM_API_KEY") or not os.getenv("LLM_BASE_URL") or not os.getenv("LLM_MODEL"):
        pytest.fail("模型验收要求显式 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL，不使用占位配置")
    tool = CalculatorTool()
    executions = []

    class LocalCalculator:
        async def get_available_tools(self):
            return {"tools": [{"name": tool.name, "description": tool.description, "parameters": tool.parameters}]}

        async def execute_tool(self, name, parameters):
            assert name == tool.name
            result = await tool.execute(parameters)
            executions.append(result)
            return result

    assistant = MCPAssistant()
    assistant.client = LocalCalculator()
    try:
        answer = await assistant.process_query("仅调用calculate计算2+3*4，只回答数字结果")
        assert answer.strip() == "14"
        assert executions and executions[0]["result"] == 14
    finally:
        await assistant.cleanup()
