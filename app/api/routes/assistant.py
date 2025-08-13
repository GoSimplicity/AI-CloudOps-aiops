#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
import asyncio
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
# 注：本文件所用请求模型均来自 app.models.request_models，移除未使用的直接导入

from app.config.settings import config
from app.db.base import session_scope
from app.db.models import AssistantSession, DocumentRecord, QueryRecord, utcnow
from app.models.request_models import (
    AutoAssistantChatReq,
    AutoAssistantDocumentReq,
    AutoAssistantSessionReq,
    AssistantDocumentUpdateReq,
)
from app.models.response_models import APIResponse
from app.utils.time_utils import iso_utc_now

# 创建日志器
logger = logging.getLogger("aiops.api.assistant")

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


router = APIRouter(tags=["assistant"])


# ============================= 历史记录 ============================= #


@router.get("/assistant/history/list")
async def assistant_history_list(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小(1-100)"),
    session_id: Optional[str] = Query(None, description="按会话ID过滤"),
    mode: Optional[str] = Query(None, description="按模式过滤(rag/mcp/doc)"),
    q: Optional[str] = Query(None, description="按问题关键字搜索"),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    """获取历史记录列表（分页、过滤、搜索；过滤软删除）。"""
    try:
        from sqlalchemy import or_, select
        from app.db.base import get_session

        with get_session() as db:
            stmt = select(QueryRecord).where(QueryRecord.deleted_at.is_(None))
            if session_id:
                stmt = stmt.where(QueryRecord.session_id == session_id)
            if mode:
                stmt = stmt.where(QueryRecord.mode == mode)
            if q:
                like = f"%{q}%"
                stmt = stmt.where(or_(QueryRecord.question.like(like), QueryRecord.answer.like(like)))
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

            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            offset = (page - 1) * size
            rows = db.execute(stmt.offset(offset).limit(size)).scalars().all()

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
            total = db.execute(select(QueryRecord).where(QueryRecord.deleted_at.is_(None))).scalars().all()
            total_count = len(total)

        return APIResponse(code=0, message="ok", data={"items": items, "total": total_count}).model_dump()
    except Exception as ex:
        logger.error(f"获取历史记录失败: {str(ex)}")
        return APIResponse(code=0, message="ok", data={"items": [], "total": 0}).model_dump()


@router.get("/assistant/history/detail/{id}")
async def assistant_history_detail(id: int):
    """获取历史记录详情。"""
    try:
        from sqlalchemy import select
        from app.db.base import get_session

        with get_session() as db:
            rec = db.execute(
                select(QueryRecord).where(QueryRecord.id == id, QueryRecord.deleted_at.is_(None))
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
        logger.error(f"获取历史记录详情失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete("/assistant/history/delete/{id}")
async def assistant_history_delete(id: int):
    """删除历史记录（软删除）。"""
    try:
        from sqlalchemy import select

        with session_scope() as db:
            rec = db.execute(select(QueryRecord).where(QueryRecord.id == id)).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            rec.deleted_at = utcnow()
            db.add(rec)
        return APIResponse(code=0, message="deleted", data={"id": id}).model_dump()
    except Exception as ex:
        logger.error(f"删除历史记录失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


# ============================= 助手实例管理 ============================= #

_assistant_agent = None
_init_lock = threading.RLock()
_is_initializing = False
_init_called = False


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


def run_async_func_in_background(func, *args, **kwargs) -> None:
    """
    将异步函数放到后台线程执行，不阻塞当前请求。
    - 在新线程中创建事件循环并运行到完成
    - 后台线程为 daemon，进程退出时自动结束
    """
    def _bg_target():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(func(*args, **kwargs))
            finally:
                loop.close()
        except Exception as ex:
            logger.error(f"后台异步任务执行失败: {str(ex)}")

    t = threading.Thread(target=_bg_target, daemon=True)
    t.start()


# 统一响应


def create_success_response(message: str, data: Optional[Dict] = None) -> Dict:
    return APIResponse(code=0, message=message, data=data or {}).model_dump()


@router.post("/assistant/chat")
async def assistant_chat(payload: AutoAssistantChatReq):
    """聊天问答（支持模式切换：1=RAG，2=MCP）。"""
    try:
        trace_id = f"req-{int(time.time()*1000)}-{os.getpid()}"
        question = (payload.query or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="query 必填")

        mode_value = int(getattr(payload, "mode", 1) or 1)
        if mode_value not in (1, 2):
            mode_value = 1
        mode_str = "rag" if mode_value == 1 else "mcp"

        # RAG 模式
        if mode_value == 1:
            logger.info(
                f"trace={trace_id} api=assistant_chat mode=rag session={payload.session_id} q_len={len(question)}"
            )
            agent = get_assistant_agent()
            if agent is None:
                raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

            # 针对平台介绍类问题，强制引导检索平台文档
            # 仅当命中平台意图时才附加提示关键词，否则保持原问题，避免过度引导
            if any(k in question for k in ["平台", "系统", "产品", "介绍", "概览", "overview", "是什么", "AI-CloudOps", "AIOps"]):
                question = f"{question} 平台概览 核心能力 架构 组件 模块"

            result = await run_async_func_safely(agent.get_answer, question, payload.session_id)

            # 清理并持久化
            cleaned_answer = sanitize_result_data(result)
            try:
                with session_scope() as db:
                    db.add(
                        QueryRecord(
                            session_id=payload.session_id,
                            question=question,
                            answer=json.dumps(cleaned_answer, ensure_ascii=False)
                            if not isinstance(cleaned_answer, str)
                            else str(cleaned_answer),
                            mode=mode_str,
                        )
                    )
            except Exception:
                pass

            # 统一响应
            if isinstance(result, dict):
                response_text = result.get("answer") or result.get("response") or ""
                confidence = result.get("confidence") or 0.8
            else:
                response_text = str(result)
                confidence = 0.8

            logger.info(
                f"trace={trace_id} api=assistant_chat mode=rag done session={payload.session_id} confidence={(result.get('confidence') if isinstance(result, dict) else 0.8)}"
            )
            return APIResponse(
                code=0,
                message="ok",
                data={"response": sanitize_for_json(response_text), "confidence": confidence},
            ).model_dump()

        # MCP 模式
        else:
            logger.info(
                f"trace={trace_id} api=assistant_chat mode=mcp session={payload.session_id} q_len={len(question)}"
            )
            try:
                from app.mcp.mcp_client import MCPAssistant
                mcp = MCPAssistant()
                response_text = await run_async_func_safely(mcp.process_query, question)
                cleaned_text = sanitize_for_json(response_text)
            except Exception as mcp_ex:
                logger.error(f"MCP处理失败: {str(mcp_ex)}")
                cleaned_text = f"MCP服务暂不可用: {str(mcp_ex)}"

            # 持久化
            try:
                with session_scope() as db:
                    db.add(
                        QueryRecord(
                            session_id=payload.session_id,
                            question=question,
                            answer=cleaned_text,
                            mode=mode_str,
                        )
                    )
            except Exception:
                pass

            logger.info(
                f"trace={trace_id} api=assistant_chat mode=mcp done session={payload.session_id}"
            )
            return APIResponse(
                code=0,
                message="ok",
                data={"response": cleaned_text, "confidence": 0.8},
            ).model_dump()
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"聊天处理失败 trace={locals().get('trace_id', 'n/a')}: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.post("/assistant/session/create")
async def assistant_session_create(body: AutoAssistantSessionReq):
    """创建会话。若未提供 session_id，则自动生成。"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        session_id = body.session_id or agent.create_session()

        # DB 幂等落库
        try:
            from sqlalchemy import select
            with session_scope() as db:
                existed = db.execute(
                    select(AssistantSession).where(AssistantSession.session_id == session_id)
                ).scalar_one_or_none()
                if not existed:
                    db.add(AssistantSession(session_id=session_id, note="auto"))
        except Exception:
            pass

        return create_success_response("会话创建成功", {"session_id": session_id, "timestamp": iso_utc_now()})
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"创建会话失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.post("/assistant/knowledge/refresh")
async def assistant_knowledge_refresh():
    """触发知识库刷新为异步后台任务，立即返回不阻塞。"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        # 清空缓存
        try:
            agent.clear_cache()
        except Exception:
            pass

        # 后台执行刷新任务
        run_async_func_in_background(agent.refresh_knowledge_base)

        return create_success_response(
            "知识库刷新任务已启动",
            {"timestamp": iso_utc_now(), "status": "started"},
        )
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"刷新知识库失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.post("/assistant/knowledge/create")
async def assistant_knowledge_create(body: AutoAssistantDocumentReq):
    """创建知识（添加到向量库并落库）。"""
    try:
        content = (body.content or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content 不能为空")

        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        title = body.title or f"Document_{int(time.time())}"
        metadata = body.metadata or {}

        # 先入向量库（带上将来用于幂等更新/删除的标识字段，DB入库后无法提前知道ID，这里先写标题兜底）
        meta_for_vec = {**(metadata or {}), "title": title}
        added = await run_async_func_safely(agent.add_document_async, content, meta_for_vec)
        if not added:
            raise HTTPException(status_code=500, detail="添加到向量库失败")

        # DB 入库
        try:
            with session_scope() as db:
                rec = DocumentRecord(
                    title=title,
                    content=content,
                    metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
                )
                db.add(rec)
                db.flush()
                # 回写 record_id 到向量库：删除基于标题的临时向量，改为使用记录ID重建，避免后续多版本
                try:
                    # 删除旧(title)映射
                    try:
                        _ = agent.vector_store_manager.delete_by_title(title)
                    except Exception:
                        pass
                    # 新建带 record_id 的文档
                    _ = asyncio.get_running_loop()
                    # 已在事件循环：走异步添加
                    _ = await agent.add_document_async(content, {**(metadata or {}), "record_id": str(rec.id), "title": title})
                except RuntimeError:
                    # 无事件循环：走安全包装
                    _ = await run_async_func_safely(agent.add_document_async, content, {**(metadata or {}), "record_id": str(rec.id), "title": title})
        except Exception:
            pass

        # 审计
        try:
            with session_scope() as db:
                db.add(
                    QueryRecord(
                        session_id=None,
                        question=f"[KNOWLEDGE_ADD] {title}",
                        answer="true",
                        mode="doc",
                    )
                )
        except Exception:
            pass

        return create_success_response(
            "知识创建成功",
            {"title": title, "content_length": len(content), "timestamp": iso_utc_now()},
        )
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"创建知识失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.put("/assistant/knowledge/update/{id}")
async def assistant_knowledge_update(id: int, body: AssistantDocumentUpdateReq):
    """更新知识：先更新DB，再对向量库执行幂等更新，移除旧版本，仅保留新内容。"""
    try:
        with session_scope() as db:
            from sqlalchemy import select
            rec = db.execute(select(DocumentRecord).where(DocumentRecord.id == id, DocumentRecord.deleted_at.is_(None))).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()

            if body.title is not None:
                rec.title = body.title
            if body.content is not None:
                rec.content = body.content
            if body.metadata is not None:
                rec.metadata_json = json.dumps(body.metadata, ensure_ascii=False)
            db.add(rec)

        # 若更新了内容：删除该记录ID历史向量，随后写入新内容，避免旧内容残留
        try:
            if body.content:
                agent = get_assistant_agent()
                # 先移除旧版本
                try:
                    deleted = agent.vector_store_manager.delete_by_record_id(str(id))
                    logger.info(f"知识更新：删除记录ID={id} 关联向量 {deleted} 条")
                except Exception as _ex:
                    logger.warning(f"删除旧向量失败（记录ID={id}）：{_ex}")

                # 再追加新版本（携带 record_id 和 title 以便后续幂等清理）
                metadata = body.metadata or {}
                metadata = {**metadata, "record_id": str(id)}
                if body.title is not None:
                    metadata.setdefault("title", body.title)
                _ = await run_async_func_safely(agent.add_document_async, body.content, metadata)
        except Exception:
            pass

        return create_success_response("知识更新成功", {"id": id, "timestamp": iso_utc_now()})
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"更新知识失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.get("/assistant/knowledge/list")
async def assistant_knowledge_list(
    page: Optional[int] = Query(1, description="页码（从1开始）"),
    size: Optional[int] = Query(20, description="每页大小(1-100)"),
    title: Optional[str] = Query(None, description="标题模糊搜索"),
):
    """获取知识列表。"""
    try:
        from sqlalchemy import select
        from app.db.base import get_session

        with get_session() as db:
            stmt = select(DocumentRecord).where(DocumentRecord.deleted_at.is_(None))
            if title:
                like = f"%{title}%"
                stmt = stmt.where(DocumentRecord.title.like(like))
            stmt = stmt.order_by(DocumentRecord.id.desc())

            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            offset = (page - 1) * size
            rows = db.execute(stmt.offset(offset).limit(size)).scalars().all()

            items = [
                {
                    "id": r.id,
                    "title": r.title,
                    "content_length": len(r.content or ""),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

            total = db.execute(select(DocumentRecord).where(DocumentRecord.deleted_at.is_(None))).scalars().all()
            total_count = len(total)

        return APIResponse(code=0, message="ok", data={"items": items, "total": total_count}).model_dump()
    except Exception as ex:
        logger.error(f"获取知识列表失败: {str(ex)}")
        return APIResponse(code=0, message="ok", data={"items": [], "total": 0}).model_dump()


@router.get("/assistant/knowledge/detail/{id}")
async def assistant_knowledge_detail(id: int):
    """获取知识详情。"""
    try:
        from sqlalchemy import select
        from app.db.base import get_session

        with get_session() as db:
            rec = db.execute(
                select(DocumentRecord).where(DocumentRecord.id == id, DocumentRecord.deleted_at.is_(None))
            ).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            data = {
                "id": rec.id,
                "title": rec.title,
                "content": rec.content,
                "metadata": json.loads(rec.metadata_json) if rec.metadata_json else None,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
                "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
            }
        return APIResponse(code=0, message="ok", data=data).model_dump()
    except Exception as ex:
        logger.error(f"获取知识详情失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.delete("/assistant/knowledge/delete/{id}")
async def assistant_knowledge_delete(id: int):
    """删除知识（软删除）。"""
    try:
        from sqlalchemy import select
        with session_scope() as db:
            rec = db.execute(select(DocumentRecord).where(DocumentRecord.id == id)).scalar_one_or_none()
            if not rec:
                return APIResponse(code=404, message="not found", data=None).model_dump()
            rec.deleted_at = utcnow()
            db.add(rec)
        return create_success_response("知识删除成功", {"id": id})
    except Exception as ex:
        logger.error(f"删除知识失败: {str(ex)}")
        return APIResponse(code=500, message="internal error", data=None).model_dump()


@router.post("/assistant/knowledge/upload")
async def assistant_knowledge_upload(file: UploadFile = File(...)):
    """上传知识库文件（保存至配置的知识库目录），随后可手动调用 refresh。"""
    try:
        kb_dir = os.path.abspath(config.rag.knowledge_base_path)
        os.makedirs(kb_dir, exist_ok=True)

        filename = file.filename or f"upload_{int(time.time())}.md"
        if not (filename.endswith(".md") or filename.endswith(".markdown") or filename.endswith(".txt")):
            raise HTTPException(status_code=400, detail="仅支持 .md/.markdown/.txt 文件")

        dest_path = os.path.join(kb_dir, filename)
        content = await file.read()
        with open(dest_path, "wb") as f:
            f.write(content)

        return create_success_response("上传成功", {"filename": filename, "path": dest_path, "size": len(content)})
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"上传知识库失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.get("/assistant/knowledge/download")
async def assistant_knowledge_download():
    """打包下载知识库目录为 zip。"""
    try:
        kb_dir = os.path.abspath(config.rag.knowledge_base_path)
        if not os.path.exists(kb_dir):
            raise HTTPException(status_code=404, detail="知识库目录不存在")

        # 内存中创建 zip
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(kb_dir):
                for name in files:
                    path = os.path.join(root, name)
                    arcname = os.path.relpath(path, kb_dir)
                    zf.write(path, arcname)
        buffer.seek(0)

        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=knowledge_base.zip",
                "Cache-Control": "no-cache",
            },
        )
    except HTTPException:
        raise
    except Exception as ex:
        logger.error(f"下载知识库失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=str(ex)) from ex


@router.post("/assistant/cache/clear")
async def assistant_cache_clear():
    """清除助手缓存。"""
    try:
        agent = get_assistant_agent()
        if agent is None:
            raise HTTPException(status_code=500, detail="智能小助手服务未正确初始化")

        result = agent.clear_cache()
        return create_success_response("缓存清理完成", {"timestamp": iso_utc_now(), **(result or {})})
    except Exception as ex:
        logger.error(f"清空缓存失败: {str(ex)}")
        raise HTTPException(status_code=500, detail=f"清除缓存失败: {str(ex)}") from ex


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
    """健康检查。"""
    try:
        # 基础健康：可扩展检查缓存和向量库
        data = {"healthy": True}
        try:
            agent = get_assistant_agent()
            if hasattr(agent, "cache_manager"):
                cache_health = agent.cache_manager.health_check()
                data["cache"] = {"status": cache_health.get("status")}
        except Exception:
            pass
        return APIResponse(code=0, message="ok", data=data).model_dump()
    except Exception as ex:
        return APIResponse(code=500, message=str(ex), data={"healthy": False}).model_dump()
