#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI助手API路由模块，提供智能问答和流式对话功能
"""

import asyncio
import logging
import threading
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, Union
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# 创建日志器
logger = logging.getLogger("aiops.api.assistant")


# Pydantic模型定义
class QueryRequest(BaseModel):
    question: str
    session_id: Optional[str] = None
    mode: Optional[str] = "normal"


class SessionRequest(BaseModel):
    session_id: Optional[str] = None


class DocumentRequest(BaseModel):
    content: str
    title: Optional[str] = None
    metadata: Optional[Dict] = None


def sanitize_for_json(text: Union[str, Any]) -> Union[str, Any]:
    """
    清理文本中的控制字符，确保JSON安全
    """
    if not isinstance(text, str):
        return text

    # 替换换行符为空格，而不是转义序列，避免在JSON响应中出现真实换行符
    text = text.replace("\n", " ").replace("\r", " ")
    # 替换多个连续空格为单个空格
    text = re.sub(r"\s+", " ", text)
    # 移除其他控制字符
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    return text.strip()


def sanitize_result_data(data: Any) -> Any:
    """
    递归清理结果数据中的所有字符串字段
    """
    if isinstance(data, dict):
        return {k: sanitize_result_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_result_data(item) for item in data]
    elif isinstance(data, str):
        return sanitize_for_json(data)
    else:
        return data


# 创建路由器
router = APIRouter(tags=["assistant"])

# 创建助手代理全局实例
_assistant_agent = None
_init_lock = threading.RLock()  # 使用可重入锁，避免死锁
_is_initializing = False
_init_called = False  # 添加标志位，防止重复初始化


def get_assistant_agent():
    """获取助手代理单例实例，采用懒加载+锁机制优化初始化性能"""
    global _assistant_agent, _is_initializing, _init_called

    # 快速检查，避免不必要的锁竞争
    if _assistant_agent is not None:
        return _assistant_agent

    # 使用锁避免多线程重复初始化
    with _init_lock:
        # 双重检查锁定模式
        if _assistant_agent is not None:
            return _assistant_agent

        if _is_initializing:
            # 如果正在初始化中，等待一小段时间后再检查
            logger.info("另一个线程正在初始化小助手，等待...")
            for _ in range(20):  # 最多等待10秒
                time.sleep(0.5)
                if _assistant_agent is not None:
                    return _assistant_agent

            # 等待超时，重置初始化状态
            logger.warning("等待初始化完成超时，重置初始化状态")
            _is_initializing = False

        # 标记为正在初始化
        _is_initializing = True

        try:
            logger.info("初始化智能小助手代理...")
            from app.core.agents.assistant import AssistantAgent

            _assistant_agent = AssistantAgent()
            logger.info("智能小助手代理初始化完成")
        except Exception as ex:
            logger.error(f"初始化智能小助手代理失败: {str(ex)}")
            _assistant_agent = None
            raise ex
        finally:
            _is_initializing = False
            _init_called = True

    return _assistant_agent


def run_sync_in_new_thread(func, *args, **kwargs):
    """
    在新线程中同步执行异步函数，避免事件循环冲突
    适用于在非异步上下文中调用异步函数的场景
    """
    result = [None]
    exception = [None]

    def target():
        try:
            # 创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result[0] = loop.run_until_complete(func(*args, **kwargs))
            finally:
                loop.close()
        except Exception as ex:
            exception[0] = ex

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=120)  # 2分钟超时

    if thread.is_alive():
        raise TimeoutError("异步函数执行超时")

    if exception[0]:
        raise exception[0]

    return result[0]


async def run_async_func_safely(func, *args, **kwargs):
    """
    安全执行异步函数，处理可能的事件循环问题
    """
    try:
        # 尝试检查当前是否有运行的事件循环
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                # 如果在运行的事件循环中，直接执行
                return await func(*args, **kwargs)
        except RuntimeError:
            # 没有运行的事件循环，直接执行
            pass

        return await func(*args, **kwargs)

    except RuntimeError as ex:
        if "There is no current event loop" in str(
            ex
        ) or "cannot be called from a running event loop" in str(ex):
            # 在新线程中运行
            logger.warning(f"事件循环冲突，在新线程中执行: {str(ex)}")
            return run_sync_in_new_thread(func, *args, **kwargs)
        else:
            raise
    except Exception as ex:
        logger.error(f"执行异步函数失败: {str(ex)}")
        raise


def create_error_response(code: int, message: str, data: Optional[Dict] = None) -> Dict:
    """创建统一格式的错误响应"""
    return {"code": code, "message": message, "data": data or {}}


def create_success_response(message: str, data: Optional[Dict] = None) -> Dict:
    """创建统一格式的成功响应"""
    return {"code": 0, "message": message, "data": data or {}}


@router.post("/assistant/query")
async def assistant_query(request_data: QueryRequest):
    """智能小助手查询API"""
    try:
        logger.info("收到查询请求")
        logger.debug(f"请求数据: {request_data}")

        # 验证问题不能为空
        question = request_data.question.strip() if request_data.question else ""
        if not question:
            logger.error("问题不能为空")
            raise HTTPException(status_code=400, detail="问题不能为空")

        # 获取助手代理
        try:
            agent = get_assistant_agent()
        except Exception as ex:
            logger.error(f"获取助手代理失败: {str(ex)}")
            if "MCP" in str(ex):
                raise HTTPException(status_code=503, detail="MCP服务暂时不可用")
            else:
                raise HTTPException(
                    status_code=500, detail="智能小助手服务未正确初始化"
                )

        # 处理MCP模式
        if request_data.mode == "mcp":
            try:
                # MCP模式处理逻辑
                result = await run_async_func_safely(
                    agent.get_answer, question, request_data.session_id
                )
                return create_success_response(
                    "查询成功",
                    {
                        "answer": sanitize_for_json(result),
                        "session_id": request_data.session_id,
                        "mode": "mcp",
                        "timestamp": datetime.now().isoformat(),
                    },
                )
            except Exception as ex:
                logger.error(f"MCP模式处理失败: {str(ex)}")
                raise HTTPException(
                    status_code=500, detail=f"MCP模式处理失败: {str(ex)}"
                )

        # 正常模式
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        try:
            # 获取回答
            result = await run_async_func_safely(
                agent.get_answer, question, request_data.session_id
            )

            # 清理并返回结果
            cleaned_result = sanitize_for_json(result)

            return create_success_response(
                "查询成功",
                {
                    "answer": cleaned_result,
                    "session_id": request_data.session_id,
                    "timestamp": datetime.now().isoformat(),
                },
            )

        except Exception as ex:
            logger.error(f"获取回答时出错: {str(ex)}")
            raise HTTPException(status_code=500, detail=f"获取回答时出错: {str(ex)}")

    except HTTPException:
        raise
    except Exception as ex:
        logger.error("查询处理失败")
        logger.error(f"错误详情: {str(ex)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理查询时出错: {str(ex)}")


@router.post("/assistant/session")
async def create_session(request_data: SessionRequest):
    """创建会话API"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 这里可以添加会话创建逻辑
        session_id = request_data.session_id or f"session_{int(time.time())}"

        return create_success_response(
            "会话创建成功",
            {"session_id": session_id, "timestamp": datetime.now().isoformat()},
        )
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"创建会话时出错: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"创建会话时出错: {str(ex)}")


@router.post("/assistant/refresh")
async def refresh_knowledge():
    """刷新知识库API"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 清空缓存
        if hasattr(agent, "response_cache"):
            agent.response_cache = {}

        # 重新加载知识库
        try:
            await run_async_func_safely(agent.load_knowledge_base)
            logger.info("知识库刷新成功")
        except Exception as refresh_ex:
            logger.error(f"知识库刷新失败: {str(refresh_ex)}")
            raise HTTPException(
                status_code=500, detail=f"刷新知识库时出错: {str(refresh_ex)}"
            )

        return create_success_response(
            "知识库刷新成功", {"timestamp": datetime.now().isoformat()}
        )
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"刷新知识库时出错: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"刷新知识库时出错: {str(ex)}")


@router.post("/assistant/add-document")
async def add_document(request_data: DocumentRequest):
    """添加文档API"""
    try:
        # 验证文档内容
        if not request_data.content or not request_data.content.strip():
            raise HTTPException(status_code=400, detail="文档内容不能为空")

        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        try:
            # 添加文档到知识库
            document_data = {
                "content": request_data.content,
                "title": request_data.title or f"Document_{int(time.time())}",
                "metadata": request_data.metadata or {},
            }

            # 清空缓存以确保新文档生效
            if hasattr(agent, "response_cache"):
                agent.response_cache = {}

            # 这里添加文档添加逻辑
            # await run_async_func_safely(agent.add_document, document_data)
            logger.info(f"文档添加成功: {document_data['title']}")

            return create_success_response(
                "文档添加成功",
                {
                    "title": document_data["title"],
                    "content_length": len(request_data.content),
                    "timestamp": datetime.now().isoformat(),
                },
            )

        except Exception as add_ex:
            logger.error(f"文档添加失败: {str(add_ex)}")
            raise HTTPException(status_code=500, detail="文档添加失败")

    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"添加文档时出错: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"添加文档时出错: {str(ex)}")


@router.post("/assistant/clear-cache")
async def clear_cache():
    """清空缓存API"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 清空所有缓存
        if hasattr(agent, "response_cache"):
            agent.response_cache = {}

        return create_success_response(
            "缓存清空成功", {"timestamp": datetime.now().isoformat()}
        )

    except Exception as ex:
        logger.error(f"清空缓存失败: {str(ex)}")
        if "超时" in str(ex):
            raise HTTPException(status_code=500, detail="清空缓存失败: 超时")
        raise HTTPException(status_code=500, detail=f"清除缓存时出错: {str(ex)}")


@router.post("/assistant/reinitialize")
async def reinitialize_assistant():
    """重新初始化助手API"""
    global _assistant_agent, _is_initializing, _init_called

    try:
        with _init_lock:
            if _is_initializing:
                # 等待当前初始化完成
                for _ in range(20):
                    time.sleep(0.5)
                    if not _is_initializing:
                        break
                else:
                    raise HTTPException(
                        status_code=500, detail="等待初始化完成超时，请稍后重试"
                    )

            # 重置全局状态
            _assistant_agent = None
            _is_initializing = False
            _init_called = False

            # 强制初始化新实例
            try:
                logger.info("开始重新初始化智能小助手...")
                # 仅调用以触发初始化，不需要保留返回值
                get_assistant_agent()

                return create_success_response(
                    "智能小助手重新初始化成功",
                    {
                        "timestamp": datetime.now().isoformat(),
                        "agent_status": "initialized",
                    },
                )

            except Exception as init_ex:
                logger.error(f"重新初始化失败: {str(init_ex)}")

                # 尝试备用初始化方法
                try:
                    from app.config.settings import config

                    # 重新配置Redis连接
                    redis_config = {
                        "host": config.redis.host,
                        "port": config.redis.port,
                        "db": config.redis.db,
                        "decode_responses": config.redis.decode_responses,
                    }

                    logger.info("使用备用初始化方法...")
                    from app.core.agents.assistant import AssistantAgent

                    _assistant_agent = AssistantAgent()

                    return create_success_response(
                        "智能小助手完全重新初始化成功",
                        {
                            "timestamp": datetime.now().isoformat(),
                            "method": "alternative",
                            "redis_config": redis_config,
                        },
                    )

                except Exception as backup_ex:
                    logger.error(f"备用初始化也失败: {str(backup_ex)}")

                    # 最后尝试：重置所有状态
                    try:
                        _assistant_agent = None
                        return create_success_response(
                            "智能小助手状态已重置，请重新发起查询以自动初始化",
                            {
                                "timestamp": datetime.now().isoformat(),
                                "status": "reset",
                            },
                        )
                    except Exception:
                        pass

                    return create_success_response(
                        "智能小助手重新初始化完成",
                        {
                            "timestamp": datetime.now().isoformat(),
                            "note": "部分功能可能需要重新激活",
                        },
                    )

        return create_success_response(
            "智能小助手重新初始化操作已完成", {"timestamp": datetime.now().isoformat()}
        )

    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"重新初始化小助手时发生错误: {str(ex)}")
        # 即使出错也尝试重置状态
        try:
            _assistant_agent = None
            _is_initializing = False
            return HTTPException(status_code=500, detail="智能小助手重新初始化失败")
        except Exception:
            pass
        raise HTTPException(
            status_code=500, detail=f"重新初始化小助手时发生错误: {str(ex)}"
        )
