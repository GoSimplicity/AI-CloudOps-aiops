#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 基于Redis的向量存储和检索系统
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class AppError(Exception):
    """通用业务异常。

    Attributes:
        message: 人类可读的错误说明
        status_code: 建议的HTTP状态码
        code: 业务错误码（0为成功，非0为错误）
    """

    message: str
    status_code: int = 400
    code: int = 1

    def __str__(self) -> str:  # pragma: no cover - 简单拼接
        return self.message


class NotFoundError(AppError):
    def __init__(self, message: str = "资源未找到"):
        super().__init__(message=message, status_code=404, code=1)


class ValidationAppError(AppError):
    def __init__(self, message: str = "参数校验失败"):
        super().__init__(message=message, status_code=422, code=1)


class ConflictError(AppError):
    def __init__(self, message: str = "资源冲突"):
        super().__init__(message=message, status_code=409, code=1)

