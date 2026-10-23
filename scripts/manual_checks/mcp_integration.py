#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops MCP集成测试脚本
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 测试RAG和MCP双模式集成
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from legacy_guard import require_legacy_opt_in
require_legacy_opt_in()

import asyncio
import os
import sys
import time
from typing import Any, Dict

import aiohttp

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


class MCPIntegrationTester:
    """MCP集成测试器"""

    def __init__(self):
        self.main_api_url = "http://localhost:8080/api/v1/assistant"
        self.mcp_server_url = "http://localhost:9000"
        self.test_results = []

    async def test_mcp_server_health(self) -> bool:
        """测试MCP服务端可用性（通过工具列表）"""
        print("🔍 测试MCP服务端健康状态...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.mcp_server_url}/tools") as response:
                    if response.status == 200:
                        data = await response.json()
                        tools = data.get("tools", [])
                        print(f"✅ MCP服务端可用，工具数量: {len(tools)}")
                        return True
                    else:
                        print(f"❌ MCP服务端可用性检查失败，状态码: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ MCP服务端连接失败: {str(e)}")
            return False

    async def test_mcp_tools_list(self) -> bool:
        """测试MCP工具列表"""
        print("🔍 测试MCP工具列表...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.mcp_server_url}/tools") as response:
                    if response.status == 200:
                        data = await response.json()
                        tools = data.get("tools", [])
                        print(f"✅ 获取到 {len(tools)} 个MCP工具:")
                        for tool in tools[:5]:  # 显示前5个工具
                            print(
                                f"   - {tool.get('name')}: {tool.get('description', 'N/A')}"
                            )
                        if len(tools) > 5:
                            print(f"   - ... 还有 {len(tools) - 5} 个工具")
                        return True
                    else:
                        print(f"❌ 获取MCP工具列表失败，状态码: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ 获取MCP工具列表失败: {str(e)}")
            return False

    async def test_rag_mode(self) -> bool:
        """测试RAG模式"""
        print("🔍 测试RAG模式...")
        try:
            async with aiohttp.ClientSession() as session:
                request_data = {
                    "question": "什么是Kubernetes Pod？",
                    "mode": 1,
                    "session_id": "test_rag_session",
                }

                async with session.post(
                    f"{self.main_api_url}/query",
                    json=request_data,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        result = data.get("data", {})
                        print("✅ RAG模式测试通过")
                        print(f"   - 模式: {result.get('mode', 'unknown')}")
                        print(f"   - 置信度: {result.get('confidence_score', 0)}")
                        print(
                            f"   - 处理时间: {result.get('processing_time', 0):.2f}秒"
                        )
                        print(f"   - 答案预览: {result.get('answer', '')[:100]}...")
                        return True
                    else:
                        print(f"❌ RAG模式测试失败，状态码: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ RAG模式测试失败: {str(e)}")
            return False

    async def test_mcp_mode(self) -> bool:
        """测试MCP模式"""
        print("🔍 测试MCP模式...")
        try:
            async with aiohttp.ClientSession() as session:
                request_data = {
                    "question": "获取当前时间",
                    "mode": 2,
                    "session_id": "test_mcp_session",
                }

                async with session.post(
                    f"{self.main_api_url}/query",
                    json=request_data,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        result = data.get("data", {})
                        print("✅ MCP模式测试通过")
                        print(f"   - 模式: {result.get('mode', 'unknown')}")
                        print(f"   - 置信度: {result.get('confidence_score', 0)}")
                        print(
                            f"   - 处理时间: {result.get('processing_time', 0):.2f}秒"
                        )
                        print(f"   - 答案: {result.get('answer', '')}")
                        return True
                    else:
                        print(f"❌ MCP模式测试失败，状态码: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ MCP模式测试失败: {str(e)}")
            return False

    async def test_health_check_unified(self) -> bool:
        """测试统一健康检查"""
        print("🔍 测试统一健康检查...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.main_api_url}/health") as response:
                    if response.status == 200:
                        data = await response.json()
                        result = data.get("data", {})
                        print("✅ 统一健康检查通过")
                        print(f"   - 服务类型: {result.get('service', 'unknown')}")

                        modes = result.get("modes", {})
                        rag_status = modes.get("rag", {}).get("status", "unknown")
                        mcp_status = modes.get("mcp", {}).get("status", "unknown")

                        print(f"   - RAG模式状态: {rag_status}")
                        print(f"   - MCP模式状态: {mcp_status}")

                        supported_modes = result.get("supported_modes", [])
                        # 使用安全的字符串拼接避免嵌套f-string语法问题
                        formatted_modes = [
                            f"{m.get('mode')}({m.get('name')})" for m in supported_modes
                        ]
                        print(f"   - 支持的模式: {formatted_modes}")

                        return True
                    else:
                        print(f"❌ 统一健康检查失败，状态码: {response.status}")
                        return False
        except Exception as e:
            print(f"❌ 统一健康检查失败: {str(e)}")
            return False

    async def test_mode_switching(self) -> bool:
        """测试模式切换"""
        print("🔍 测试模式切换...")

        # 测试从RAG到MCP的切换
        session_id = "mode_switch_test"

        try:
            async with aiohttp.ClientSession() as session:
                # 先用RAG模式
                rag_request = {
                    "question": "什么是容器？",
                    "mode": 1,
                    "session_id": session_id,
                }

                async with session.post(
                    f"{self.main_api_url}/query",
                    json=rag_request,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    rag_result = await response.json()

                # 再用MCP模式
                mcp_request = {
                    "question": "获取当前时间",
                    "mode": 2,
                    "session_id": session_id,
                }

                async with session.post(
                    f"{self.main_api_url}/query",
                    json=mcp_request,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    mcp_result = await response.json()

                # 检查结果
                rag_mode = rag_result.get("data", {}).get("mode")
                mcp_mode = mcp_result.get("data", {}).get("mode")

                if rag_mode == "rag" and mcp_mode == "mcp":
                    print("✅ 模式切换测试通过")
                    print(f"   - 第一次调用模式: {rag_mode}")
                    print(f"   - 第二次调用模式: {mcp_mode}")
                    return True
                else:
                    print("❌ 模式切换测试失败")
                    print("   - 期望: rag -> mcp")
                    print(f"   - 实际: {rag_mode} -> {mcp_mode}")
                    return False

        except Exception as e:
            print(f"❌ 模式切换测试失败: {str(e)}")
            return False

    async def run_all_tests(self) -> Dict[str, Any]:
        """运行所有测试"""
        print("🚀 开始MCP集成测试")
        print("=" * 50)

        tests = [
            ("MCP服务端健康检查", self.test_mcp_server_health()),
            ("MCP工具列表", self.test_mcp_tools_list()),
            ("RAG模式", self.test_rag_mode()),
            ("MCP模式", self.test_mcp_mode()),
            ("统一健康检查", self.test_health_check_unified()),
            ("模式切换", self.test_mode_switching()),
        ]

        results = {}
        passed = 0
        total = len(tests)

        for test_name, test_coro in tests:
            print()
            try:
                result = await test_coro
                results[test_name] = result
                if result:
                    passed += 1
            except Exception as e:
                print(f"❌ {test_name} 测试异常: {str(e)}")
                results[test_name] = False

        print()
        print("=" * 50)
        print("📊 测试结果汇总")
        print(f"总测试数: {total}")
        print(f"通过数: {passed}")
        print(f"失败数: {total - passed}")
        print(f"成功率: {passed / total * 100:.1f}%")

        if passed == total:
            print("🎉 所有测试通过！MCP集成成功！")
        else:
            print("⚠️  部分测试失败，请检查服务状态")

        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "success_rate": passed / total,
            "details": results,
        }


async def main():
    """主函数"""
    print("AI-CloudOps MCP集成测试")
    print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 检查服务是否启动的提示
    print("⚠️  请确保以下服务已启动:")
    print("   1. 主服务: python -m app.main")
    print("   2. MCP服务: python -m app.mcp.main")
    print()

    input("按回车键开始测试...")
    print()

    tester = MCPIntegrationTester()
    results = await tester.run_all_tests()

    return results


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n测试被用户中断")
    except Exception as e:
        print(f"\n测试执行失败: {str(e)}")
        sys.exit(1)
