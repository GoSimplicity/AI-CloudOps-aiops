#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps主应用程序入口
"""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import signal
import sys
import time
from typing import Any, Dict

from fastapi import FastAPI

# 添加项目根目录到系统路径
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from app.api.middleware import register_middleware
from app.api.routes import register_routes
from app.common.constants import AppConstants, ServiceConstants
from app.config.logging import setup_logging
from app.config.settings import config
from app.services.factory import ServiceFactory
from app.services.startup import StartupService

# 全局启动服务实例
startup_service = StartupService()


# 全局状态管理
class AppState:
    """应用状态管理"""

    def __init__(self):
        self.is_shutting_down = False
        self.active_requests = 0
        self.shutdown_event = asyncio.Event()
        self.max_shutdown_wait = ServiceConstants.MAX_SHUTDOWN_WAIT


app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger = logging.getLogger("aiops")
    logger.info("=" * 50)
    logger.info(f"{AppConstants.APP_NAME} 启动中...")
    logger.info(f"版本: {AppConstants.APP_VERSION}")
    logger.info(f"调试模式: {config.debug}")
    logger.info(f"日志级别: {config.log_level}")
    logger.info("=" * 50)

    # 初始化启动服务
    await startup_service.initialize()

    # 注册需要管理的服务实例（保持原有机制）
    await _register_managed_services()

    # 通过ServiceFactory预热关键服务（懒初始化已经异步触发，这里仅确保健康）
    try:
        factory_health = await ServiceFactory.health()
        logger.info(f"ServiceFactory 管理的服务健康状态: {factory_health}")
    except Exception as e:
        logger.warning(f"获取ServiceFactory健康状态失败: {e}")

    # 启动预热机制
    warmup_results = await startup_service.warmup_services()

    # 记录启动信息
    startup_time = startup_service.get_uptime()
    logger.info(f"{AppConstants.APP_NAME} 启动完成，耗时: {startup_time:.2f}秒")
    logger.info(f"服务地址: http://{config.host}:{config.port}")
    logger.info("主要API端点:")
    logger.info(
        f"  - GET  {AppConstants.API_VERSION_V1}/predict/ready - 预测服务就绪检查"
    )
    logger.info(
        f"  - GET  {AppConstants.API_VERSION_V1}/rca/ready     - RCA服务就绪检查"
    )
    logger.info(
        f"  - GET  {AppConstants.API_VERSION_V1}/autofix/ready - 自动修复服务就绪检查"
    )
    logger.info(
        f"  - GET  {AppConstants.API_VERSION_V1}/assistant/ready - 智能助手就绪检查"
    )
    logger.info(f"  - POST {AppConstants.API_VERSION_V1}/predict       - 负载预测")
    logger.info(f"  - POST {AppConstants.API_VERSION_V1}/rca           - 根因分析")
    logger.info(f"  - POST {AppConstants.API_VERSION_V1}/autofix       - 自动修复")
    logger.info(
        f"  - POST {AppConstants.API_VERSION_V1}/assistant/query - AI-CloudOps智能小助手"
    )

    if not warmup_results["success"]:
        logger.warning("部分服务预热失败，系统仍可正常使用")

    yield

    # 关闭时执行 - 优雅关闭逻辑
    logger.info("开始优雅关闭流程...")
    app_state.is_shutting_down = True

    # 等待活跃请求完成
    start_wait = time.time()
    while app_state.active_requests > 0:
        wait_time = time.time() - start_wait
        if wait_time > app_state.max_shutdown_wait:
            logger.warning(
                f"等待超时，强制关闭。剩余活跃请求: {app_state.active_requests}"
            )
            break
        logger.info(f"等待 {app_state.active_requests} 个请求完成...")
        await asyncio.sleep(0.5)

    # 清理资源
    await cleanup_resources()

    # 工厂统一清理（在启动服务清理之后执行，确保路由持有的单例也被关闭）
    try:
        factory_cleanup = await ServiceFactory.cleanup_all()
        if factory_cleanup.get("success", False):
            logger.info("ServiceFactory 所有服务清理完成")
        else:
            logger.warning(f"ServiceFactory 清理存在失败: {factory_cleanup}")
    except Exception as e:
        logger.warning(f"ServiceFactory 清理失败: {e}")

    total_time = startup_service.get_uptime()
    logger.info(f"{AppConstants.APP_NAME} 运行总时长: {total_time:.2f}秒")
    logger.info(f"{AppConstants.APP_NAME} 已优雅关闭")


async def cleanup_resources():
    """清理资源"""
    logger = logging.getLogger("aiops")

    try:
        logger.info("开始资源清理流程...")

        # 使用StartupService清理所有注册的服务
        cleanup_results = await startup_service.cleanup_all_services()

        if cleanup_results["success"]:
            logger.info("所有服务清理完成")
        else:
            logger.warning("部分服务清理失败，但将继续关闭流程")
            for service_name, result in cleanup_results["services"].items():
                if not result.get("success"):
                    logger.warning(
                        f"服务 {service_name} 清理失败: {result.get('error', '未知错误')}"
                    )
            for instance_name, result in cleanup_results["instances"].items():
                if not result.get("success"):
                    logger.warning(
                        f"实例 {instance_name} 清理失败: {result.get('error', '未知错误')}"
                    )

        # 额外的全局资源清理
        await _cleanup_global_resources()

        logger.info("资源清理流程完成")

    except Exception as e:
        logger.error(f"资源清理失败: {str(e)}")
        # 即使清理失败，也要继续关闭流程


async def _cleanup_global_resources():
    """清理全局资源"""
    logger = logging.getLogger("aiops")

    try:
        # 清理任何剩余的全局资源
        # 例如：关闭日志处理器、清理临时文件等

        # 清理缓存连接池（如果有全局实例）
        try:
            pass
            # 这里可以添加全局缓存管理器的清理逻辑
            logger.debug("缓存资源清理检查完成")
        except Exception as e:
            logger.debug(f"缓存资源清理检查时出错: {e}")

        # 强制垃圾回收
        import gc

        gc.collect()
        logger.debug("垃圾回收完成")

    except Exception as e:
        logger.warning(f"全局资源清理时出错: {e}")


async def _register_managed_services():
    """注册需要管理的服务"""
    logger = logging.getLogger("aiops")

    try:
        logger.info("开始注册需要管理的服务...")

        # 服务注册配置
        services_to_register = [
            {
                "name": "prediction",
                "module": "app.services.prediction_service",
                "class": "PredictionService",
                "required": False,
            },
            {
                "name": "rca",
                "module": "app.services.rca_service",
                "class": "RCAService",
                "required": False,
            },
            {
                "name": "autofix",
                "module": "app.services.autofix_service",
                "class": "AutoFixService",
                "required": False,
            },
            {
                "name": "assistant",
                "module": "app.services.assistant_service",
                "class": "OptimizedAssistantService",
                "required": False,
            },
            {
                "name": "mcp",
                "module": "app.services.mcp_service",
                "class": "MCPService",
                "required": False,
            },
        ]

        # 注册BaseService类型的服务
        for service_config in services_to_register:
            await _register_service(service_config, logger)

        # 注册其他关键实例
        await _register_additional_instances(logger)

        logger.info(
            f"服务注册完成，共注册 {len(startup_service._managed_services)} 个服务"
        )

    except Exception as e:
        logger.error(f"服务注册过程出错: {e}")


async def _register_service(service_config: Dict[str, Any], logger) -> bool:
    """注册单个服务"""
    try:
        # 动态导入模块
        module = __import__(
            service_config["module"], fromlist=[service_config["class"]]
        )
        service_class = getattr(module, service_config["class"])

        # 创建服务实例
        service_instance = service_class()

        # 初始化服务
        await service_instance.initialize()

        # 注册到startup_service
        startup_service.register_service(service_instance)

        logger.debug(f"{service_config['name']}服务注册成功")
        return True

    except Exception as e:
        if service_config.get("required", False):
            logger.error(f"{service_config['name']}服务注册失败（必需服务）: {e}")
            raise
        else:
            logger.warning(f"{service_config['name']}服务注册失败（可选服务）: {e}")
            return False


async def _register_additional_instances(logger) -> None:
    """注册其他服务实例"""
    # 注册企业助手实例
    try:
        from app.core.agents.enterprise_assistant import get_enterprise_assistant

        assistant_instance = await get_enterprise_assistant()
        if assistant_instance:
            startup_service.register_service_instance(
                "enterprise_assistant", assistant_instance
            )
            logger.debug("企业助手实例注册成功")
    except Exception as e:
        logger.warning(f"企业助手实例注册失败: {e}")

    # 注册Redis缓存管理器实例（如果有全局实例）
    try:
        # 这里可以添加全局缓存管理器的注册逻辑
        logger.debug("缓存管理器实例检查完成")
    except Exception as e:
        logger.debug(f"缓存管理器实例注册失败: {e}")


def create_app() -> FastAPI:
    """
    创建FastAPI应用实例

    Returns:
        FastAPI: 配置好的FastAPI应用实例
    """
    # 创建FastAPI应用
    app = FastAPI(
        title=AppConstants.APP_NAME,
        description=AppConstants.APP_DESCRIPTION,
        version=AppConstants.APP_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    # 设置日志系统
    setup_logging(app)

    # 注册中间件（包含请求计数中间件）
    register_middleware_with_counter(app)

    # 注册路由
    try:
        register_routes(app)
        logger = logging.getLogger("aiops")
        logger.info("路由注册完成")
    except Exception as e:
        logger = logging.getLogger("aiops")
        logger.error(f"路由注册失败: {str(e)}")
        raise

    # 自定义OpenAPI元数据（分类标签）
    app.openapi_tags = [
        {
            "name": "prediction",
            "description": "预测服务API，包括QPS、CPU、内存、磁盘预测",
        },
        {"name": "assistant", "description": "智能助手API，包括问答、会话、知识库管理"},
        {"name": "rca", "description": "根因分析API"},
        {"name": "autofix", "description": "自动修复API"},
        {"name": "cache", "description": "缓存管理API，包括统计、清理、配置与性能"},
    ]

    return app


def register_middleware_with_counter(app: FastAPI):
    """注册中间件（包含请求计数）"""
    from fastapi import Request, Response

    @app.middleware("http")
    async def request_counter_middleware(request: Request, call_next):
        """请求计数中间件"""
        # 如果正在关闭，拒绝新请求
        if app_state.is_shutting_down:
            return Response(
                content="Service is shutting down",
                status_code=503,
                headers={"Retry-After": "60"},
            )

        # 增加活跃请求计数
        app_state.active_requests += 1

        try:
            response = await call_next(request)
            return response
        finally:
            # 减少活跃请求计数
            app_state.active_requests -= 1

    # 注册其他中间件
    try:
        register_middleware(app)
        logger = logging.getLogger("aiops")
        logger.info("中间件注册完成")
    except Exception as e:
        logger = logging.getLogger("aiops")
        logger.error(f"中间件注册失败: {str(e)}")
        raise


# 创建应用实例
app = create_app()


class GracefulShutdown:
    """优雅关闭处理器"""

    def __init__(self):
        self.should_exit = False
        self.logger = logging.getLogger("aiops")
        self._shutdown_started = False
        self._shutdown_lock = asyncio.Lock()

    def signal_handler(self, sig, frame):
        """信号处理函数"""
        signal_name = signal.Signals(sig).name
        self.logger.info(f"收到信号 {signal_name}，准备优雅关闭...")

        if not self._shutdown_started:
            self._shutdown_started = True
            self.should_exit = True
            app_state.shutdown_event.set()

            # 尝试启动优雅关闭流程
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 如果事件循环正在运行，创建任务来处理关闭
                    loop.create_task(self._handle_graceful_shutdown())
                else:
                    # 如果事件循环未运行，直接设置退出标志
                    self.logger.warning("事件循环未运行，设置退出标志")
            except Exception as e:
                self.logger.error(f"启动优雅关闭流程失败: {e}")
        else:
            self.logger.warning(f"已经开始关闭流程，忽略信号 {signal_name}")

    async def _handle_graceful_shutdown(self):
        """处理优雅关闭流程"""
        async with self._shutdown_lock:
            try:
                self.logger.info("开始执行优雅关闭流程...")

                # 等待短暂时间让当前请求完成
                await asyncio.sleep(1)

                # 设置应用为关闭状态
                app_state.is_shutting_down = True

                # 等待活跃请求完成
                start_wait = time.time()
                while app_state.active_requests > 0:
                    wait_time = time.time() - start_wait
                    if wait_time > app_state.max_shutdown_wait:
                        self.logger.warning(
                            f"等待超时，强制关闭。剩余活跃请求: {app_state.active_requests}"
                        )
                        break
                    self.logger.info(f"等待 {app_state.active_requests} 个请求完成...")
                    await asyncio.sleep(0.5)

                # 执行资源清理
                await cleanup_resources()

                self.logger.info("优雅关闭流程完成")

            except Exception as e:
                self.logger.error(f"优雅关闭流程失败: {e}")
            finally:
                # 强制退出
                self.logger.info("正在退出应用...")
                os._exit(0)

    def setup_signal_handlers(self):
        """设置信号处理器"""
        if sys.platform != "win32":
            # Unix/Linux 系统
            signal.signal(signal.SIGTERM, self.signal_handler)
            signal.signal(signal.SIGINT, self.signal_handler)
            signal.signal(signal.SIGHUP, self.signal_handler)
            signal.signal(signal.SIGUSR1, self._status_handler)  # 状态查询信号
        else:
            # Windows 系统
            signal.signal(signal.SIGTERM, self.signal_handler)
            signal.signal(signal.SIGINT, self.signal_handler)

    def _status_handler(self, sig, frame):
        """状态查询信号处理器（SIGUSR1）"""
        try:
            self.logger.info("=" * 50)
            self.logger.info("应用状态报告:")
            self.logger.info(f"  运行时长: {startup_service.get_uptime():.2f}秒")
            self.logger.info(f"  活跃请求: {app_state.active_requests}")
            self.logger.info(f"  正在关闭: {app_state.is_shutting_down}")
            self.logger.info(f"  已注册服务: {len(startup_service._managed_services)}")

            # 尝试获取服务状态
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._log_services_status())
            except Exception as e:
                self.logger.info(f"  无法获取详细服务状态: {e}")

            self.logger.info("=" * 50)
        except Exception as e:
            self.logger.error(f"状态报告失败: {e}")

    async def _log_services_status(self):
        """记录服务状态"""
        try:
            services_status = await startup_service.get_services_status()
            self.logger.info("  服务状态:")
            for service_name, status in services_status.items():
                self.logger.info(f"    - {service_name}: {status}")
        except Exception as e:
            self.logger.error(f"获取服务状态失败: {e}")


if __name__ == "__main__":
    """直接运行时的主入口"""
    from uvicorn import Config, Server

    logger = logging.getLogger("aiops")

    # 创建优雅关闭处理器
    shutdown_handler = GracefulShutdown()
    shutdown_handler.setup_signal_handlers()

    try:
        # 使用应用配置的日志级别，确保一致性
        uvicorn_log_level = config.log_level.lower()
        
        config_uvicorn = Config(
            app="app.main:app",
            host=config.host,
            port=config.port,
            reload=config.debug,
            reload_dirs=["app", "config"] if config.debug else None,
            reload_excludes=(
                ["logs", "data", "__pycache__", "*.pyc"] if config.debug else None
            ),
            log_level=uvicorn_log_level,
            access_log=True,
            reload_delay=0.25 if config.debug else None,
            # 禁用uvicorn自己的日志配置
            log_config=None,
        )

        server = Server(config_uvicorn)

        logger.info(f"在 {config.host}:{config.port} 启动FastAPI服务器")

        # 启动服务器
        if sys.platform != "win32":
            # Unix/Linux: 使用异步运行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def serve():
                await server.serve()

            try:
                loop.run_until_complete(serve())
            except KeyboardInterrupt:
                logger.info("收到中断信号，正在优雅关闭...")
            finally:
                loop.close()
        else:
            # Windows: 使用同步运行
            server.run()

    except KeyboardInterrupt:
        logger.info("收到中断信号，正在优雅关闭服务...")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
        raise
    finally:
        logger.info("服务已完全停止")
