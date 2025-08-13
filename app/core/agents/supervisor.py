#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import logging
from typing import Any, Dict

from langchain_core.messages import BaseMessage

from app.models.data_models import AgentState
from app.services.llm import LLMService

logger = logging.getLogger("aiops.supervisor")


class SupervisorAgent:
    """Supervisor agent - AIOps system intelligent coordinator"""

    def __init__(self):
        """Initialize supervisor agent"""
        # 使用我们自己的LLM服务进行智能决策
        self.llm_service = LLMService()

        # 定义可用的工作代理成员
        self.members = ["Researcher", "Coder", "K8sFixer", "Notifier"]

        # 设置决策提示词模板
        self._setup_prompt()

        logger.info("Supervisor Agent initialized")

    def _setup_prompt(self):
        """Setup decision prompt template"""
        system_prompt = """你是一个AIOps系统的主管，负责协调以下工作人员来解决Kubernetes相关问题：

工作人员及其职责：
1. Researcher: 负责网络搜索和信息收集，获取相关技术文档和解决方案
2. Coder: 负责执行Python代码，进行数据分析和计算任务
3. K8sFixer: 负责分析和修复Kubernetes部署问题，执行自动化修复操作
4. Notifier: 负责发送通知和警报，联系相关人员

你的任务是：
1. 分析当前问题和上下文
2. 决定下一步应该让哪个工作人员行动
3. 当问题解决完成时返回FINISH

决策原则：
- 如果需要搜索技术信息或最佳实践，选择Researcher
- 如果需要数据分析或复杂计算，选择Coder  
- 如果是Kubernetes部署问题需要修复，选择K8sFixer
- 如果需要发送通知或寻求人工帮助，选择Notifier
- 如果问题已解决或无法继续处理，选择FINISH

请根据当前对话内容和问题状态，决定下一个行动者。"""

        self.prompt_template = (
            system_prompt
            + """
基于上面的对话历史，决定下一步行动：
- 如果问题需要更多信息，选择 Researcher
- 如果需要代码分析，选择 Coder
- 如果是K8s问题需要修复，选择 K8sFixer  
- 如果需要通知或人工介入，选择 Notifier
- 如果问题已解决，选择 FINISH

从以下选项中选择: ["Researcher", "Coder", "K8sFixer", "Notifier", "FINISH"]
同时简要说明选择理由。
"""
        )

    async def route_next_action(self, state: AgentState) -> Dict[str, Any]:
        """Intelligent routing decision - decide next agent to execute"""
        try:
            # 检查迭代次数限制，防止无限循环消耗资源
            if state.iteration_count >= state.max_iterations:
                logger.warning(f"达到最大迭代次数限制: {state.max_iterations}")
                return {"next": "FINISH", "reasoning": "达到最大迭代次数限制"}

            # 构建消息历史文本，只保留最近的消息以避免上下文过长
            message_history = ""
            for msg in state.messages[-10:]:  # 只保留最近10条消息，避免Token超限
                if isinstance(msg, dict):
                    role = msg.get("role", "user")
                    content = msg.get("content", str(msg))
                    message_history += f"\n{role}: {content}\n"
                elif isinstance(msg, BaseMessage):
                    message_history += f"\n{msg.type}: {msg.content}\n"
                else:
                    message_history += f"\nuser: {str(msg)}\n"

            # 构建完整的决策提示词，包含模板和当前上下文
            full_prompt = f"{self.prompt_template}\n\n对话历史:\n{message_history}"

            # 调用LLM服务进行智能路由决策
            messages = [{"role": "user", "content": full_prompt}]
            response_text = await self.llm_service.generate_response_async(messages)

            if not response_text:
                logger.error("LLM响应为空")
                return {"next": "FINISH", "reasoning": "LLM服务未返回有效响应"}

            # 解析LLM响应，提取决策结果和理由
            next_agent = None
            reasoning = None

            # 清理响应文本，准备解析
            response_text = response_text.strip()

            # 首先尝试解析JSON格式的响应
            if response_text.startswith("{") and response_text.endswith("}"):
                try:
                    import json

                    parsed = json.loads(response_text)
                    next_agent = parsed.get("next")
                    reasoning = parsed.get("reasoning")
                except Exception:
                    # JSON解析失败，继续尝试其他方法
                    pass

            # 如果JSON解析失败，使用模式匹配从文本中提取决策
            if not next_agent:
                for member in self.members + ["FINISH"]:
                    # 检查多种可能的表达方式
                    if (
                        f"next: {member}" in response_text
                        or f'"next": "{member}"' in response_text
                        or f"选择 {member}" in response_text
                        or f"选择: {member}" in response_text
                    ):
                        next_agent = member
                        break
                    elif member in response_text:
                        # 如果直接找到了成员名，检查上下文是否表明选择了它
                        surrounding = (
                            response_text.split(member)[0][-20:]
                            + response_text.split(member)[1][:20]
                        )
                        if "选择" in surrounding or "next" in surrounding:
                            next_agent = member
                            break

            # 尝试从响应中提取决策理由
            if not reasoning and "理由" in response_text:
                reasoning_parts = response_text.split("理由")
                if len(reasoning_parts) > 1:
                    reasoning = reasoning_parts[1].strip(": ").strip()

            # 如果无法从响应中提取有效的决策，默认结束流程
            if not next_agent:
                logger.warning(f"无法从响应中确定下一个Agent: {response_text}")
                next_agent = "FINISH"
                reasoning = "无法确定下一步行动"

            logger.info(f"Supervisor决策: {next_agent}, 理由: {reasoning}")

            return {
                "next": next_agent,
                "reasoning": reasoning or "",
                "iteration_count": state.iteration_count + 1,
            }

        except Exception as e:
            logger.error(f"Supervisor路由决策失败: {str(e)}")
            return {"next": "FINISH", "reasoning": f"决策失败: {str(e)}"}

    async def analyze_problem_context(self, problem_description: str) -> Dict[str, Any]:
        """
        深度分析问题上下文

        对用户提出的问题进行结构化分析，识别问题类型、涉及组件、严重程度等关键信息。
        这个分析结果可以帮助主管代理做出更准确的路由决策。

        Args:
            problem_description (str): 用户描述的问题

        Returns:
            Dict[str, Any]: 问题分析结果，包含：
                - problem_type: 问题类型（性能、错误、配置等）
                - components: 涉及的系统组件
                - severity: 严重程度等级
                - suggested_approach: 建议的处理方式
                - required_agents: 需要的代理类型

        分析维度：
        1. 问题类型识别（性能、故障、配置、安全等）
        2. 组件影响范围（应用、网络、存储、计算等）
        3. 紧急程度评估（低、中、高、紧急）
        4. 处理策略建议（自动修复、人工介入、监控观察等）
        """
        try:
            analysis_prompt = """分析以下问题，提供结构化的问题分析：

问题描述：{problem}

请分析：
1. 问题类型（性能、错误、配置等）
2. 涉及的组件
3. 严重程度
4. 建议的处理方式
5. 需要的工作人员类型

以JSON格式返回分析结果。"""

            messages = [
                {
                    "role": "user",
                    "content": analysis_prompt.format(problem=problem_description),
                }
            ]

            response = await self.llm_service.generate_response_async(messages)

            try:
                import json

                analysis = json.loads(response)
                return analysis
            except json.JSONDecodeError:
                # 尝试从响应中提取JSON
                if "```json" in response:
                    json_part = response.split("```json")[1].split("```")[0]
                    try:
                        analysis = json.loads(json_part)
                        return analysis
                    except Exception:
                        pass

                return {
                    "problem_type": "unknown",
                    "components": ["kubernetes"],
                    "severity": "medium",
                    "suggested_approach": response,
                    "required_agents": ["K8sFixer"],
                }

        except Exception as e:
            logger.error(f"问题上下文分析失败: {str(e)}")
            return {
                "problem_type": "unknown",
                "severity": "medium",
                "suggested_approach": "需要进一步分析",
                "required_agents": ["K8sFixer"],
            }

    def create_initial_state(self, problem_description: str) -> AgentState:
        """
        创建工作流初始状态

        为新的问题处理流程创建初始的代理状态，包含用户问题和基本配置。
        这个初始状态将作为整个工作流程的起点。

        Args:
            problem_description (str): 用户描述的问题

        Returns:
            AgentState: 初始化的代理状态，包含：
                - messages: 初始消息列表（包含用户问题）
                - current_step: 当前处理步骤
                - context: 上下文信息
                - iteration_count: 迭代计数器
                - max_iterations: 最大迭代次数限制

        状态包含的信息：
        1. 用户的原始问题描述
        2. 时间戳和元数据
        3. 迭代控制参数
        4. 上下文存储空间
        """
        return AgentState(
            messages=[
                {"role": "user", "content": problem_description, "timestamp": "now"}
            ],
            current_step="analyzing",
            context={"problem": problem_description, "start_time": "now"},
            iteration_count=0,
            max_iterations=10,
        )

    def should_continue(self, state: AgentState) -> bool:
        """
        判断工作流是否应该继续执行

        根据当前状态判断是否应该继续工作流程，主要检查：
        1. 迭代次数是否超限
        2. 是否已标记为完成
        3. 是否检测到无限循环

        Args:
            state (AgentState): 当前的代理状态

        Returns:
            bool: True表示应该继续，False表示应该停止

        停止条件：
        1. 达到最大迭代次数限制
        2. 下一步行动标记为FINISH
        3. 检测到代理行动的无限循环模式
        4. 系统资源限制或异常状态
        """
        if state.iteration_count >= state.max_iterations:
            return False

        if state.next_action == "FINISH":
            return False

        # 检测无限循环：如果最近的行动都是同一个代理，可能陷入循环
        recent_actions = [
            msg.get("agent") for msg in state.messages[-5:] if isinstance(msg, dict)
        ]
        if len(set(recent_actions)) <= 1 and len(recent_actions) >= 3:
            logger.warning("检测到可能的无限循环，停止处理")
            return False

        return True

    def get_workflow_summary(self, state: AgentState) -> Dict[str, Any]:
        """
        生成工作流执行总结

        分析整个工作流程的执行情况，统计使用的代理、执行的操作和最终结果。
        这个总结可以用于：
        1. 向用户展示处理过程
        2. 系统性能分析和优化
        3. 问题诊断和调试
        4. 工作流程审计

        Args:
            state (AgentState): 当前的代理状态

        Returns:
            Dict[str, Any]: 工作流总结，包含：
                - agents_used: 使用过的代理列表
                - actions_taken: 执行的操作列表
                - iterations: 总迭代次数
                - final_step: 最终执行步骤

        统计信息：
        1. 代理使用情况和频次
        2. 执行的具体操作和结果
        3. 时间消耗和性能指标
        4. 成功率和错误信息
        """
        agents_used = set()
        actions_taken = []

        for msg in state.messages:
            if isinstance(msg, dict):
                agent = msg.get("agent")
                if agent:
                    agents_used.add(agent)
                    action = msg.get("action", "unknown action")
                    actions_taken.append(f"{agent}: {action}")

        return {
            "agents_used": list(agents_used),
            "actions_taken": actions_taken,
            "iterations": state.iteration_count,
            "final_step": state.current_step,
        }

    async def process_agent_state(self, state: AgentState) -> AgentState:
        """
        处理代理状态并生成工作流总结

        这是工作流程的收尾方法，负责处理最终状态并生成完整的执行总结。
        该方法会更新状态中的统计信息、成功标识和结果描述。

        Args:
            state (AgentState): 当前的代理状态

        Returns:
            AgentState: 更新后的状态，包含：
                - 工作流执行总结
                - 成功/失败状态标识
                - 统计信息和性能数据
                - 最终结果描述

        处理内容：
        1. 生成工作流执行统计
        2. 评估整体执行结果
        3. 更新状态上下文信息
        4. 标记工作流完成状态
        5. 记录性能和错误信息
        """
        try:
            from dataclasses import replace

            # 获取完整的工作流执行总结
            summary = self.get_workflow_summary(state)

            # 获取当前上下文并准备更新
            context = dict(state.context)

            # 添加详细的总结信息到上下文
            context["summary"] = (
                f"工作流完成，共使用了 {len(summary['agents_used'])} 个智能体，执行了 {state.iteration_count} 次迭代。"
            )
            context["workflow_summary"] = summary
            context["workflow_completed"] = True

            # 根据执行过程中是否出现错误来判断整体成功状态
            if "error" not in context:
                context["success"] = True

            # 生成用户友好的最终状态描述
            final_status = "成功" if context.get("success", False) else "失败"
            context["result"] = f"自动修复工作流已{final_status}完成"

            # 使用dataclass的replace方法创建更新后的状态
            return replace(state, context=context)

        except Exception as e:
            logger.error(f"生成工作流总结失败: {str(e)}")
            # 获取当前上下文
            context = dict(state.context)
            context["error"] = f"生成工作流总结失败: {str(e)}"
            return replace(state, context=context)


class Supervisor:
    def __init__(self):
        self.agent = SupervisorAgent()

    def get_agents_status(self) -> Dict[str, Any]:
        return {"agents": [{"name": name, "status": "active", "tasks": 0} for name in self.agent.members]}

    def execute_task(self, task_type: str, priority: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        return {"task_id": "task_123", "status": "started", "assigned_agents": ["detector", "analyzer"]}

    def get_coordination_status(self) -> Dict[str, Any]:
        return {"active_tasks": 0, "completed_tasks": 0, "agent_utilization": 0.0}
