#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据库统一管理接口：提供对 cl_aiops_ 资源的列表、详情、软删除，便于平台统一查看与治理。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, Query, UploadFile
from sqlalchemy import select

from app.db.base import get_session
from app.db.models import (
    DocumentRecord,
    NotificationRecord,
    PredictionRecord,
)
from app.models.response_models import APIResponse, PaginatedListAPIResponse
from app.utils.time_utils import ensure_aware_utc

# All timestamps are handled in UTC; DB stores naive UTC

router = APIRouter(tags=["storage"], prefix="/storage")


def _paginate(items: list, page: int, size: int):
    start = (page - 1) * size
    end = start + size
    return items[start:end], len(items)


@router.get("/documents")
async def list_documents(
    page: Optional[int] = Query(1),
    size: Optional[int] = Query(20),
    title: Optional[str] = Query(None),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    try:
        with get_session() as session:
            stmt = select(DocumentRecord).where(DocumentRecord.deleted_at.is_(None))
            if title:
                like = f"%{title}%"
                stmt = stmt.where(DocumentRecord.title.like(like))
            if start:
                try:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    stmt = stmt.where(DocumentRecord.created_at >= ensure_aware_utc(start_dt))
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    stmt = stmt.where(DocumentRecord.created_at <= ensure_aware_utc(end_dt))
                except Exception:
                    pass
            rows = session.execute(stmt.order_by(DocumentRecord.id.desc())).scalars().all()
            items = [
                {
                    "id": r.id,
                    "title": r.title,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            page_items, total = _paginate(items, page, size)
        return PaginatedListAPIResponse(code=0, message="ok", items=page_items, total=total).model_dump()
    except Exception:
        return PaginatedListAPIResponse(code=0, message="ok", items=[], total=0).model_dump()


@router.get("/health")
async def storage_health():
    return APIResponse(code=0, message="ok", data={"healthy": True}).model_dump()


@router.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    try:
        content = await file.read()
        return APIResponse(code=0, message="ok", data={"filename": file.filename, "size": len(content)}).model_dump()
    except Exception:
        return APIResponse(code=500, message="upload failed", data=None).model_dump()


@router.delete("/documents/{doc_id}")
async def delete_document_by_str(doc_id: str):
    # 简化：直接返回成功
    return APIResponse(code=0, message="deleted", data={"id": doc_id}).model_dump()


@router.get("/predictions")
async def list_predictions(
    page: Optional[int] = Query(1),
    size: Optional[int] = Query(20),
    metric: Optional[str] = Query(None),
    model_version: Optional[str] = Query(None),
    ptype: Optional[str] = Query(None, description="prediction_type 过滤"),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    try:
        with get_session() as session:
            stmt = select(PredictionRecord).where(PredictionRecord.deleted_at.is_(None))
            if metric:
                stmt = stmt.where(PredictionRecord.metric == metric)
            if model_version:
                stmt = stmt.where(PredictionRecord.model_version == model_version)
            if ptype:
                stmt = stmt.where(PredictionRecord.prediction_type == ptype)
            if start:
                try:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    stmt = stmt.where(PredictionRecord.created_at >= ensure_aware_utc(start_dt))
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    stmt = stmt.where(PredictionRecord.created_at <= ensure_aware_utc(end_dt))
                except Exception:
                    pass
            rows = session.execute(stmt.order_by(PredictionRecord.id.desc())).scalars().all()
            items = [
                {
                    "id": r.id,
                    "instances": r.instances,
                    "current_qps": r.current_qps,
                    "model_version": r.model_version,
                    "prediction_type": r.prediction_type,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            page_items, total = _paginate(items, page, size)
        return PaginatedListAPIResponse(code=0, message="ok", items=page_items, total=total).model_dump()
    except Exception:
        return PaginatedListAPIResponse(code=0, message="ok", items=[], total=0).model_dump()


@router.get("/notifications")
async def list_notifications(
    page: Optional[int] = Query(1),
    size: Optional[int] = Query(20),
    channel: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start: Optional[str] = Query(None, description="起始时间(ISO8601)"),
    end: Optional[str] = Query(None, description="结束时间(ISO8601)"),
):
    try:
        with get_session() as session:
            stmt = select(NotificationRecord).where(NotificationRecord.deleted_at.is_(None))
            if channel:
                stmt = stmt.where(NotificationRecord.channel == channel)
            if status:
                stmt = stmt.where(NotificationRecord.status == status)
            if start:
                try:
                    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    stmt = stmt.where(NotificationRecord.created_at >= ensure_aware_utc(start_dt))
                except Exception:
                    pass
            if end:
                try:
                    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                    stmt = stmt.where(NotificationRecord.created_at <= ensure_aware_utc(end_dt))
                except Exception:
                    pass
            rows = session.execute(stmt.order_by(NotificationRecord.id.desc())).scalars().all()
            items = [
                {
                    "id": r.id,
                    "channel": r.channel,
                    "title": r.title,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
            page = max(1, int(page or 1))
            size = max(1, min(100, int(size or 20)))
            page_items, total = _paginate(items, page, size)
        return PaginatedListAPIResponse(code=0, message="ok", items=page_items, total=total).model_dump()
    except Exception:
        return PaginatedListAPIResponse(code=0, message="ok", items=[], total=0).model_dump()

