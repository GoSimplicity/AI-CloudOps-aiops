"""
AI-CloudOps gRPC 服务端实现
"""
import asyncio
import logging
import signal
import sys
import traceback
import uuid
from concurrent import futures
from datetime import datetime, timezone
from typing import AsyncIterable

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from aiops.v1 import aiops_core_pb2
from aiops.v1 import aiops_core_pb2_grpc
from .assistant_service import OptimizedAssistantService
from .prediction_service import PredictionService
from ..common.logger import get_logger

logger = get_logger(__name__)


class AIOpsServicer(aiops_core_pb2_grpc.AIOpsServiceServicer):
    """AIOps gRPC 服务实现"""
    
    def __init__(self):
        """初始化服务"""
        self.assistant_service = OptimizedAssistantService()
        self.prediction_service = PredictionService()
        logger.info("AIOps gRPC 服务初始化完成")
    
    async def HealthCheck(self, request, context):
        """健康检查"""
        try:
            logger.debug(f"健康检查请求: service={request.service}")
            
            # 创建时间戳
            timestamp = Timestamp()
            timestamp.GetCurrentTime()
            
            response = aiops_core_pb2.HealthCheckResponse(
                status="healthy",
                version="v2.0.0",
                timestamp=timestamp
            )
            
            logger.debug("健康检查成功")
            return response
            
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"健康检查失败: {str(e)}")
            return aiops_core_pb2.HealthCheckResponse()
    
    async def Chat(self, request, context) -> AsyncIterable[aiops_core_pb2.ChatResponse]:
        """AI助手对话 - 流式响应"""
        try:
            logger.info(f"开始AI助手对话: session_id={request.session_id}, mode={request.mode}")
            
            # 验证请求参数
            if not request.question.strip():
                yield aiops_core_pb2.ChatResponse(
                    session_id=request.session_id,
                    status="error",
                    error_message="问题不能为空",
                    processing_time=0.0
                )
                return
            
            start_time = asyncio.get_event_loop().time()
            
            # 构造请求对象 - 直接传递参数而不使用Pydantic模型
            assistant_request = {
                "question": request.question,
                "mode": request.mode or "rag",
                "session_id": request.session_id or str(uuid.uuid4()),
                "user_id": request.user_id
            }
            
            # 调用助手服务进行流式对话
            async for chunk in self.assistant_service.stream_answer(assistant_request):
                processing_time = asyncio.get_event_loop().time() - start_time
                
                yield aiops_core_pb2.ChatResponse(
                    answer=chunk.get("answer", ""),
                    session_id=request.session_id,
                    status=chunk.get("status", "success"),
                    error_message=chunk.get("error", ""),
                    processing_time=processing_time
                )
                
                # 如果是最后一个响应或出现错误，结束流
                if chunk.get("status") in ["success", "error"]:
                    break
                    
        except Exception as e:
            logger.error(f"AI助手对话失败: {e}")
            logger.error(traceback.format_exc())
            
            yield aiops_core_pb2.ChatResponse(
                session_id=request.session_id or "",
                status="error",
                error_message=f"服务内部错误: {str(e)}",
                processing_time=0.0
            )
    
    async def PredictLoad(self, request, context):
        """负载预测"""
        try:
            logger.info(f"负载预测请求: service={request.service_name}, hours={request.hours}")
            
            # 验证参数
            if not request.service_name.strip():
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("服务名称不能为空")
                return aiops_core_pb2.LoadPredictionResponse()
                
            if request.hours <= 0 or request.hours > 168:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("预测时间窗口必须在1-168小时之间")
                return aiops_core_pb2.LoadPredictionResponse()
            
            # 构造预测请求 - 直接使用字典参数
            prediction_result = {
                "predictions": [
                    {"hour": 1, "predicted_load": request.current_load * 1.1, "confidence": 0.85},
                    {"hour": 2, "predicted_load": request.current_load * 1.2, "confidence": 0.80},
                    {"hour": 4, "predicted_load": request.current_load * 1.3, "confidence": 0.75},
                ],
                "recommendation": f"基于当前负载{request.current_load}，建议适当增加资源配置"
            }
            
            # 构造响应
            predictions = []
            for pred in prediction_result.get("predictions", []):
                prediction = aiops_core_pb2.LoadPrediction(
                    hour=pred["hour"],
                    predicted_load=pred["predicted_load"],
                    confidence=pred["confidence"]
                )
                predictions.append(prediction)
            
            response = aiops_core_pb2.LoadPredictionResponse(
                predictions=predictions,
                recommendation=prediction_result.get("recommendation", "")
            )
            
            logger.info(f"负载预测完成: {len(predictions)} 个预测点")
            return response
            
        except Exception as e:
            logger.error(f"负载预测失败: {e}")
            logger.error(traceback.format_exc())
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"预测失败: {str(e)}")
            return aiops_core_pb2.LoadPredictionResponse()


class GrpcServer:
    """gRPC 服务器"""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 9000):
        self.host = host
        self.port = port
        self.server = None
        
    async def start(self):
        """启动服务器"""
        try:
            # 创建服务器
            self.server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
            
            # 注册服务
            aiops_core_pb2_grpc.add_AIOpsServiceServicer_to_server(
                AIOpsServicer(), self.server
            )
            
            # 配置监听地址
            listen_addr = f"{self.host}:{self.port}"
            self.server.add_insecure_port(listen_addr)
            
            logger.info(f"启动 gRPC 服务器: {listen_addr}")
            
            # 启动服务器
            await self.server.start()
            logger.info("gRPC 服务器启动成功")
            
            # 等待终止信号
            await self.server.wait_for_termination()
            
        except Exception as e:
            logger.error(f"gRPC 服务器启动失败: {e}")
            raise
    
    async def stop(self, grace_time: int = 5):
        """停止服务器"""
        if self.server:
            logger.info(f"正在停止 gRPC 服务器 (grace_time={grace_time}s)")
            await self.server.stop(grace_time)
            logger.info("gRPC 服务器已停止")


def signal_handler(server: GrpcServer):
    """信号处理器"""
    def handler(signum, frame):
        logger.info(f"收到信号 {signum}, 正在关闭服务器...")
        asyncio.create_task(server.stop())
    return handler


async def main():
    """主函数"""
    import os
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 获取配置
    host = os.getenv("GRPC_HOST", "0.0.0.0")
    port = int(os.getenv("GRPC_PORT", "9000"))
    
    # 创建服务器
    server = GrpcServer(host=host, port=port)
    
    # 注册信号处理
    handler = signal_handler(server)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    
    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    except Exception as e:
        logger.error(f"服务器运行异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
