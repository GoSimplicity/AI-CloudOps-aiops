"""
MCP服务市场数据模型
定义服务、健康状态和配置的数据结构
"""

from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime


class ServiceStatus(str, Enum):
    """服务状态枚举"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"
    STARTING = "starting"
    STOPPING = "stopping"


class ServiceHealth(str, Enum):
    """服务健康状态"""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class MCPService(BaseModel):
    """MCP服务定义"""
    
    name: str = Field(..., description="服务名称，唯一标识")
    display_name: str = Field(..., description="显示名称")
    description: str = Field(..., description="服务描述")
    version: str = Field(default="1.0.0", description="服务版本")
    server_url: str = Field(..., description="MCP服务地址")
    timeout: int = Field(default=30, description="请求超时时间(秒)")
    max_retries: int = Field(default=3, description="最大重试次数")
    health_check_interval: int = Field(default=60, description="健康检查间隔(秒)")
    weight: int = Field(default=1, description="负载权重")
    tags: List[str] = Field(default_factory=list, description="服务标签")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="额外元数据")
    
    # 运行时状态
    status: ServiceStatus = Field(default=ServiceStatus.INACTIVE)
    health: ServiceHealth = Field(default=ServiceHealth.UNKNOWN)
    last_health_check: Optional[datetime] = Field(default=None)
    registered_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    
    class Config:
        use_enum_values = True


class ServiceInstance(BaseModel):
    """服务实例信息"""
    
    service: MCPService
    available_tools: List[Dict[str, Any]] = Field(default_factory=list)
    last_check: datetime = Field(default_factory=datetime.now)
    consecutive_failures: int = Field(default=0)
    total_requests: int = Field(default=0)
    successful_requests: int = Field(default=0)
    error_rate: float = Field(default=0.0)
    
    
class ServiceConfig(BaseModel):
    """服务配置"""
    
    services: List[MCPService] = Field(default_factory=list)
    global_timeout: int = Field(default=30, description="全局超时时间")
    max_concurrent_requests: int = Field(default=100, description="最大并发请求数")
    health_check_enabled: bool = Field(default=True, description="是否启用健康检查")
    load_balancing: str = Field(default="round_robin", description="负载均衡策略")
    cache_enabled: bool = Field(default=True, description="是否启用缓存")
    cache_ttl: int = Field(default=300, description="缓存TTL(秒)")


class ToolRequest(BaseModel):
    """工具调用请求"""
    
    tool_name: str = Field(..., description="工具名称")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="工具参数")
    preferred_services: List[str] = Field(default_factory=list, description="优先使用的服务")
    timeout: Optional[int] = Field(default=None, description="请求超时")


class ToolResponse(BaseModel):
    """工具调用响应"""
    
    success: bool = Field(..., description="是否成功")
    result: Any = Field(default=None, description="调用结果")
    service_name: str = Field(..., description="处理请求的服务名称")
    response_time: float = Field(..., description="响应时间(秒)")
    error: Optional[str] = Field(default=None, description="错误信息")


class ServiceHealthReport(BaseModel):
    """服务健康报告"""
    
    service_name: str = Field(..., description="服务名称")
    status: ServiceStatus = Field(..., description="服务状态")
    health: ServiceHealth = Field(..., description="健康状态")
    response_time: Optional[float] = Field(default=None, description="响应时间")
    last_check: Optional[datetime] = Field(default=None, description="最后检查时间")
    error_message: Optional[str] = Field(default=None, description="错误信息")
    available_tools: List[str] = Field(default_factory=list, description="可用工具列表")


class MarketStatus(BaseModel):
    """市场状态总览"""
    
    total_services: int = Field(..., description="总服务数")
    active_services: int = Field(..., description="活跃服务数")
    healthy_services: int = Field(..., description="健康服务数")
    total_tools: int = Field(..., description="总工具数")
    services: List[ServiceHealthReport] = Field(default_factory=list)
    last_update: datetime = Field(default_factory=datetime.now)