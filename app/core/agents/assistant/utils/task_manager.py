#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
任务管理模块 - 管理异步任务
"""

import asyncio
import logging
import threading
from asyncio import CancelledError

logger = logging.getLogger("aiops.assistant.task_manager")


class TaskManager:
    """管理异步任务，确保它们能够正确完成或取消"""

    def __init__(self):
        self._tasks = set()
        self._lock = threading.Lock()
        self._shutdown = False

    def create_task(self, coro, description="未命名任务"):
        """创建并管理异步任务"""
        if self._shutdown:
            logger.debug(f"任务管理器已关闭，忽略任务: {description}")
            return None

        async def wrapped_coro():
            try:
                await coro
                logger.debug(f"异步任务 '{description}' 完成")
            except CancelledError:
                logger.debug(f"异步任务 '{description}' 被取消")
            except Exception as e:
                logger.error(f"异步任务 '{description}' 执行失败: {e}")
            finally:
                with self._lock:
                    if task in self._tasks:
                        self._tasks.remove(task)

        task = asyncio.create_task(wrapped_coro())

        with self._lock:
            self._tasks.add(task)

        return task

    async def shutdown(self, timeout=5.0):
        """关闭任务管理器，等待或取消所有任务"""
        self._shutdown = True

        with self._lock:
            tasks = self._tasks.copy()

        if not tasks:
            return

        logger.debug(f"等待 {len(tasks)} 个任务完成...")

        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
            )
            logger.debug("所有任务已完成")
        except asyncio.TimeoutError:
            logger.warning(f"等待任务完成超时，强制取消 {len(tasks)} 个任务")
            for task in tasks:
                if not task.done():
                    task.cancel()

            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=1.0
                )
            except asyncio.TimeoutError:
                logger.warning("部分任务取消操作超时")

        with self._lock:
            self._tasks.clear()


# 全局任务管理器
_task_manager = None


def get_task_manager():
    """获取全局任务管理器"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager


def create_safe_task(coro, description="未命名任务"):
    """创建安全的异步任务"""
    manager = get_task_manager()
    return manager.create_task(coro, description)
