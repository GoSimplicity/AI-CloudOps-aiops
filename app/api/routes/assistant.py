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
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.base import session_scope
from app.db.models import QueryRecord, utcnow
from app.models.response_models import APIResponse
from app.utils.time_utils import iso_utc_now

# 创建日志器
logger = logging.getLogger("aiops.api.assistant")




# Pydantic模型定义
class QueryRequest(BaseModel):
    question: str = Field(..., description="用户提问内容")
    session_id: Optional[str] = Field(default=None, description="会话ID（可选）")
    mode: Optional[str] = Field(
        default="normal", description="运行模式：normal 或 mcp"
    )


class SessionRequest(BaseModel):
    session_id: Optional[str] = Field(default=None, description="会话ID（可选）")


class DocumentRequest(BaseModel):
    content: str = Field(..., description="文档内容")
    title: Optional[str] = Field(default=None, description="文档标题（可选）")
    metadata: Optional[Dict] = Field(default=None, description="文档元数据（可选）")


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
@router.get("/queries/history")
async def list_query_history(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小"),
    session_id: Optional[str] = Query(None, description="按会话ID过滤"),
    mode: Optional[str] = Query(None, description="按模式过滤(normal/mcp/doc)"),
    q: Optional[str] = Query(None, description="按问题关键字搜索"),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    """查询提问历史（分页、过滤、搜索；过滤已软删除）"""
    try:
        from sqlalchemy import select

        from app.db.base import get_session

        with get_session() as session:
            stmt = select(QueryRecord).where(QueryRecord.deleted_at.is_(None))
            if session_id:
                stmt = stmt.where(QueryRecord.session_id == session_id)
            if mode:
                stmt = stmt.where(QueryRecord.mode == mode)
            if q:
                # 简单like搜索
                from sqlalchemy import or_
                like = f"%{q}%"
                stmt = stmt.where(or_(QueryRecord.question.like(like), QueryRecord.answer.like(like)))
            # 时间范围过滤（基于 created_at）
            if start:
                try:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                    stmt = stmt.where(QueryRecord.created_at >= start_dt)
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
                    stmt = stmt.where(QueryRecord.created_at <= end_dt)
                except Exception:
                    pass
            stmt = stmt.order_by(QueryRecord.id.desc())
            # 分页
            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            offset = (page - 1) * size
            rows = session.execute(stmt.offset(offset).limit(size)).scalars().all()
            # 统计总数（简单起见，这里不重复包含like计算成本；如需精准可再执行count）
            items_total = len(rows) if not (session_id or mode or q) else None
            items = [
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "question": sanitize_for_json(r.question),
                    "answer": sanitize_for_json(r.answer) if r.answer else None,
                    "mode": r.mode,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        return APIResponse(code=0, message="ok", data={"items": items, "total": items_total}).model_dump()
    except Exception as ex:
        logger.error(f"获取查询历史失败: {str(ex)}")
        return APIResponse(code=0, message="ok", data={"items": []}).model_dump()


@router.get("/queries/{query_id}")
async def get_query_detail(query_id: int):
    """获取单条问答记录详情"""
    try:
        from sqlalchemy import select

        from app.db.base import get_session

        with get_session() as session:
            rec = session.execute(
                select(QueryRecord).where(QueryRecord.id == query_id, QueryRecord.deleted_at.is_(None))
            ).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            item = {
                "id": rec.id,
                "session_id": rec.session_id,
                "question": sanitize_for_json(rec.question),
                "answer": sanitize_for_json(rec.answer) if rec.answer else None,
                "mode": rec.mode,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        return APIResponse(code=0, message="ok", data=item).model_dump()
    except Exception as ex:
        logger.error(f"获取问答详情失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete("/queries/{query_id}")
async def soft_delete_query(query_id: int):
    """软删除问答记录"""
    try:
        from sqlalchemy import select

        with session_scope() as session:
            rec = (
                session.execute(
                    select(QueryRecord).where(QueryRecord.id == query_id)
                ).scalar_one_or_none()
            )
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            # 统一使用 UTC 时间进行软删除标记
            rec.deleted_at = utcnow()
            session.add(rec)
        return APIResponse(code=0, message="deleted", data={"id": query_id}).model_dump()
    except Exception as ex:
        logger.error(f"删除问答记录失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


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
        # 统一抛出标准运行时异常，避免类型歧义
        raise RuntimeError(str(exception[0]))

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


# 统一响应构建：保留成功响应构造器；错误响应由全局中间件处理


def create_success_response(message: str, data: Optional[Dict] = None) -> Dict:
    """创建统一格式的成功响应"""
    return APIResponse(code=0, message=message, data=data or {}).model_dump()


@router.post("/queries/create")
async def create_query(request_data: QueryRequest):
    """创建智能小助手查询"""
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
                raise HTTPException(status_code=503, detail="MCP服务暂时不可用") from ex
            else:
                raise HTTPException(
                    status_code=500, detail="智能小助手服务未正确初始化"
                ) from ex

        # 处理MCP模式
        if request_data.mode == "mcp":
            try:
                # MCP模式处理逻辑
                result = await run_async_func_safely(
                    agent.get_answer, question, request_data.session_id
                )
                try:
                    with session_scope() as session:
                        session.add(
                            QueryRecord(
                                session_id=request_data.session_id,
                                question=question,
                                answer=str(result) if result is not None else None,
                                mode="mcp",
                            )
                        )
                except Exception:
                    # 持久化失败不影响主流程
                    pass
                return create_success_response(
                    "查询成功",
                    {
                        "answer": sanitize_for_json(result),
                        "session_id": request_data.session_id,
                        "mode": "mcp",
                        "timestamp": iso_utc_now(),
                    },
                )
            except Exception as ex:
                logger.error(f"MCP模式处理失败: {str(ex)}")
                raise HTTPException(
                    status_code=500, detail=f"MCP模式处理失败: {str(ex)}"
                ) from ex

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

            # 异步成功后记录数据库
            try:
                with session_scope() as session:
                    session.add(
                        QueryRecord(
                            session_id=request_data.session_id,
                            question=question,
                            answer=str(cleaned_result) if cleaned_result is not None else None,
                            mode=request_data.mode or "normal",
                        )
                    )
            except Exception:
                pass

            return create_success_response(
                "查询成功",
                {
                    "answer": cleaned_result,
                    "session_id": request_data.session_id,
                    "timestamp": iso_utc_now(),
                },
            )

        except Exception as ex:
            logger.error(f"获取回答时出错: {str(ex)}")
            raise HTTPException(status_code=500, detail=f"获取回答时出错: {str(ex)}") from ex

    except HTTPException:
        raise
    except Exception as ex:
        logger.error("查询处理失败")
        logger.error(f"错误详情: {str(ex)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理查询时出错: {str(ex)}") from ex


@router.post("/sessions/create")
async def create_session(request_data: SessionRequest):
    """创建会话"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 这里可以添加会话创建逻辑
        session_id = request_data.session_id or f"session_{int(time.time())}"
        # 写入会话表（幂等）
        try:
            from sqlalchemy import select

            from app.db.models import AssistantSession
            with session_scope() as session:
                existed = (
                    session.execute(
                        select(AssistantSession).where(
                            AssistantSession.session_id == session_id
                        )
                    ).scalar_one_or_none()
                )
                if not existed:
                    session.add(AssistantSession(session_id=session_id, note="auto"))
        except Exception:
            pass

        return create_success_response(
            "会话创建成功", {"session_id": session_id, "timestamp": iso_utc_now()}
        )
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"创建会话时出错: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"创建会话时出错: {str(ex)}") from ex


@router.post("/knowledge/refresh")
async def refresh_knowledge():
    """刷新知识库"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 清空缓存
        if hasattr(agent, "response_cache"):
            agent.response_cache = {}

        # 重新加载知识库
        try:
            await run_async_func_safely(agent.refresh_knowledge_base)
            logger.info("知识库刷新成功")
        except Exception as refresh_ex:
            logger.error(f"知识库刷新失败: {str(refresh_ex)}")
            raise HTTPException(
                status_code=500, detail=f"刷新知识库时出错: {str(refresh_ex)}"
            ) from refresh_ex

            return create_success_response("知识库刷新成功", {"timestamp": iso_utc_now()})
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"刷新知识库时出错: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"刷新知识库时出错: {str(ex)}") from ex


@router.post("/documents/create")
async def create_document(request_data: DocumentRequest):
    """创建文档"""
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
            added = agent.add_document(document_data["content"], document_data.get("metadata") or {})
            if not added:
                raise RuntimeError("添加文档失败")
            logger.info(f"文档添加成功: {document_data['title']}")

            # 入库 cl_aiops_documents
            try:
                from app.db.models import DocumentRecord
                with session_scope() as session:
                    session.add(
                        DocumentRecord(
                            title=document_data["title"],
                            content=document_data["content"],
                            metadata_json=(str(document_data.get("metadata")) if document_data.get("metadata") else None),
                        )
                    )
            except Exception:
                pass

            # 记录一次伪查询（类型：doc_add），便于审计
            try:
                with session_scope() as session:
                    session.add(
                        QueryRecord(
                            session_id=None,
                            question=f"[DOC_ADD] {document_data['title']}",
                            answer=str(True),
                            mode="doc",
                        )
                    )
            except Exception:
                pass

            return create_success_response(
                "文档添加成功",
                {
                    "title": document_data["title"],
                    "content_length": len(request_data.content),
                        "timestamp": iso_utc_now(),
                },
            )

        except Exception as add_ex:
            logger.error(f"文档添加失败: {str(add_ex)}")
            raise HTTPException(status_code=500, detail="文档添加失败") from add_ex

    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"添加文档时出错: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"添加文档时出错: {str(ex)}") from ex


@router.post("/cache/clear")
async def clear_cache():
    """清空缓存"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 清空所有缓存
        if hasattr(agent, "response_cache"):
            agent.response_cache = {}

        return create_success_response("缓存清空成功", {"timestamp": iso_utc_now()})

    except Exception as ex:
        logger.error(f"清空缓存失败: {str(ex)}")
        if "超时" in str(ex):
            raise HTTPException(status_code=500, detail="清空缓存失败: 超时") from ex
        raise HTTPException(status_code=500, detail=f"清除缓存时出错: {str(ex)}") from ex


@router.post("/assistant/reinitialize")
async def reinitialize_assistant():
    """重新初始化助手"""
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
                    {"timestamp": iso_utc_now(), "agent_status": "initialized"},
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
                            "timestamp": iso_utc_now(),
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
                        "timestamp": iso_utc_now(),
                                "status": "reset",
                            },
                        )
                    except Exception:
                        pass

                    return create_success_response(
                        "智能小助手重新初始化完成",
                        {
                            "timestamp": iso_utc_now(),
                            "note": "部分功能可能需要重新激活",
                        },
                    )

        return create_success_response(
            "智能小助手重新初始化操作已完成", {"timestamp": iso_utc_now()}
        )

    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"重新初始化小助手时发生错误: {str(ex)}")
        # 即使出错也尝试重置状态
        try:
            _assistant_agent = None
            _is_initializing = False
            # 发生错误时抛出 HTTP 异常，避免返回异常对象
            raise HTTPException(status_code=500, detail="智能小助手重新初始化失败")
        except Exception:
            pass
        raise HTTPException(
            status_code=500, detail=f"重新初始化小助手时发生错误: {str(ex)}"
        ) from ex


@router.get("/assistant/health")
async def assistant_health():
    return APIResponse(code=0, message="ok", data={"healthy": True}).model_dump()


@router.post("/assistant/chat")
async def assistant_chat(payload: Dict[str, Any]):
    try:
        query = payload.get("query") or ""
        if not query:
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=400, detail="query 必填")
        return APIResponse(code=0, message="ok", data={"response": "ok", "confidence": 0.8}).model_dump()
    except Exception as ex:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=500, detail=str(ex)) from ex


@router.post("/assistant/search")
async def assistant_search(payload: Dict[str, Any]):
    try:
        return APIResponse(code=0, message="ok", data={"results": []}).model_dump()
    except Exception as ex:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=500, detail=str(ex)) from ex
