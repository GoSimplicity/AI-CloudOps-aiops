#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MCP市场API路由
提供MCP服务的管理、发现和工具调用功能
Author: Claude
License: Apache 2.0
"""

import logging
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, Field

from app.mcp.market.registry import MCPRegistry
from app.mcp.market.plugin_manager import MCPPluginManager
from app.mcp.market.models import MCPService, ToolRequest, ToolResponse
from app.mcp.market.service_discovery import ServiceDiscovery

logger = logging.getLogger("aiops.api.mcp_market")

# 创建路由实例
router = APIRouter(prefix="/mcp/market", tags=["MCP市场"])

# 全局服务注册中心和插件管理器
_registry: Optional[MCPRegistry] = None
_plugin_manager: Optional[MCPPluginManager] = None
_service_discovery: Optional[ServiceDiscovery] = None


class ServiceCreateRequest(BaseModel):
    """创建服务请求"""
    name: str = Field(..., description="服务名称，唯一标识")
    display_name: str = Field(..., description="显示名称")
    description: str = Field(..., description="服务描述")
    server_url: str = Field(..., description="MCP服务地址")
    version: str = Field(default="1.0.0", description="服务版本")
    timeout: int = Field(default=30, description="请求超时时间(秒)")
    max_retries: int = Field(default=3, description="最大重试次数")
    health_check_interval: int = Field(default=60, description="健康检查间隔(秒)")
    weight: int = Field(default=1, description="负载权重")
    tags: List[str] = Field(default_factory=list, description="服务标签")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="额外元数据")


class ServiceUpdateRequest(BaseModel):
    """更新服务请求"""
    display_name: Optional[str] = Field(None, description="显示名称")
    description: Optional[str] = Field(None, description="服务描述")
    server_url: Optional[str] = Field(None, description="MCP服务地址")
    version: Optional[str] = Field(None, description="服务版本")
    timeout: Optional[int] = Field(None, description="请求超时时间(秒)")
    max_retries: Optional[int] = Field(None, description="最大重试次数")
    health_check_interval: Optional[int] = Field(None, description="健康检查间隔(秒)")
    weight: Optional[int] = Field(None, description="负载权重")
    tags: Optional[List[str]] = Field(None, description="服务标签")
    metadata: Optional[Dict[str, Any]] = Field(None, description="额外元数据")


class ServiceListResponse(BaseModel):
    """服务列表响应"""
    services: List[MCPService]
    total: int
    active: int
    healthy: int


class ToolExecuteRequest(BaseModel):
    """工具执行请求"""
    tool_name: str = Field(..., description="工具名称")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="工具参数")
    preferred_services: List[str] = Field(default_factory=list, description="优先使用的服务")
    timeout: Optional[int] = Field(None, description="请求超时")


class MarketStatusResponse(BaseModel):
    """市场状态响应"""
    total_services: int
    active_services: int
    healthy_services: int
    total_tools: int
    services: List[Dict[str, Any]]


def get_registry() -> MCPRegistry:
    """获取注册中心实例"""
    global _registry
    if _registry is None:
        _registry = MCPRegistry()
    return _registry


def get_plugin_manager() -> MCPPluginManager:
    """获取插件管理器实例"""
    global _plugin_manager
    if _plugin_manager is None:
        from app.mcp.market.registry import MCPRegistry
        registry = get_registry()
        _plugin_manager = MCPPluginManager(registry)
    return _plugin_manager


def get_service_discovery() -> ServiceDiscovery:
    """获取服务发现实例"""
    global _service_discovery
    if _service_discovery is None:
        from app.mcp.market.service_discovery import ServiceDiscovery
        registry = get_registry()
        _service_discovery = ServiceDiscovery(registry)
    return _service_discovery


@router.on_event("startup")
async def startup_event():
    """启动时初始化"""
    try:
        registry = get_registry()
        plugin_manager = get_plugin_manager()
        
        await registry.start()
        await plugin_manager.start()
        
        logger.info("MCP市场服务已启动")
    except Exception as e:
        logger.error(f"MCP市场启动失败: {e}")


@router.on_event("shutdown")
async def shutdown_event():
    """关闭时清理"""
    try:
        if _registry:
            await _registry.stop()
        if _plugin_manager:
            await _plugin_manager.stop()
        logger.info("MCP市场服务已停止")
    except Exception as e:
        logger.error(f"MCP市场关闭失败: {e}")


@router.get("/status", response_model=MarketStatusResponse)
async def get_market_status():
    """获取MCP市场状态"""
    try:
        registry = get_registry()
        status = registry.get_market_status()
        return MarketStatusResponse(**status)
    except Exception as e:
        logger.error(f"获取市场状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/services", response_model=ServiceListResponse)
async def list_services(
    tags: Optional[List[str]] = Query(None, description="按标签过滤"),
    status: Optional[str] = Query(None, description="按状态过滤"),
    search: Optional[str] = Query(None, description="搜索关键词")
):
    """获取服务列表"""
    try:
        registry = get_registry()
        services = registry.list_services()
        
        # 应用过滤条件
        filtered_services = services
        
        if tags:
            filtered_services = [
                s for s in filtered_services
                if any(tag in s.tags for tag in tags)
            ]
        
        if status:
            filtered_services = [
                s for s in filtered_services
                if s.status.value == status
            ]
        
        if search:
            search_lower = search.lower()
            filtered_services = [
                s for s in filtered_services
                if search_lower in s.name.lower() or 
                   search_lower in s.display_name.lower() or
                   search_lower in s.description.lower()
            ]
        
        active_count = len([s for s in filtered_services if s.status.value == "active"])
        healthy_count = len([s for s in filtered_services if s.health.value == "healthy"])
        
        return ServiceListResponse(
            services=filtered_services,
            total=len(filtered_services),
            active=active_count,
            healthy=healthy_count
        )
    except Exception as e:
        logger.error(f"获取服务列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/services", response_model=MCPService)
async def create_service(request: ServiceCreateRequest):
    """创建新服务"""
    try:
        plugin_manager = get_plugin_manager()
        
        # 创建服务实例
        service = MCPService(
            name=request.name,
            display_name=request.display_name,
            description=request.description,
            server_url=request.server_url,
            version=request.version,
            timeout=request.timeout,
            max_retries=request.max_retries,
            health_check_interval=request.health_check_interval,
            weight=request.weight,
            tags=request.tags,
            metadata=request.metadata
        )
        
        # 添加到配置
        success = await plugin_manager.add_service(service)
        if not success:
            raise HTTPException(status_code=400, detail="添加服务失败")
        
        return service
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"创建服务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/services/{service_name}", response_model=MCPService)
async def get_service(service_name: str):
    """获取单个服务详情"""
    try:
        registry = get_registry()
        service = registry.get_service(service_name)
        
        if not service:
            raise HTTPException(status_code=404, detail="服务未找到")
        
        return service
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取服务详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/services/{service_name}", response_model=MCPService)
async def update_service(service_name: str, request: ServiceUpdateRequest):
    """更新服务配置"""
    try:
        plugin_manager = get_plugin_manager()
        
        # 构建更新参数字典
        update_data = request.dict(exclude_unset=True)
        if not update_data:
            raise HTTPException(status_code=400, detail="没有提供更新数据")
        
        success = await plugin_manager.update_service_config(service_name, **update_data)
        if not success:
            raise HTTPException(status_code=404, detail="服务未找到")
        
        # 返回更新后的服务
        registry = get_registry()
        service = registry.get_service(service_name)
        return service
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新服务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/services/{service_name}")
async def delete_service(service_name: str):
    """删除服务"""
    try:
        plugin_manager = get_plugin_manager()
        success = await plugin_manager.remove_service(service_name)
        
        if not success:
            raise HTTPException(status_code=404, detail="服务未找到")
        
        return {"message": f"服务 {service_name} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除服务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/services/{service_name}/tools")
async def get_service_tools(service_name: str):
    """获取服务的可用工具"""
    try:
        registry = get_registry()
        tools = await registry.get_service_tools(service_name)
        
        if service_name not in [s.name for s in registry.list_services()]:
            raise HTTPException(status_code=404, detail="服务未找到")
        
        return {"service_name": service_name, "tools": tools}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取服务工具失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools")
async def get_all_tools():
    """获取所有服务的工具"""
    try:
        registry = get_registry()
        tools = await registry.get_all_tools()
        
        return {"tools": tools}
    except Exception as e:
        logger.error(f"获取所有工具失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tools/execute")
async def execute_tool(request: ToolExecuteRequest):
    """执行工具调用（智能路由）"""
    try:
        service_discovery = get_service_discovery()
        
        # 查找能够处理该工具的服务
        result = await service_discovery.execute_tool(
            tool_name=request.tool_name,
            parameters=request.parameters,
            preferred_services=request.preferred_services,
            timeout=request.timeout
        )
        
        if not result.success:
            raise HTTPException(status_code=400, detail=result.error)
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"执行工具失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/services/{service_name}/refresh")
async def refresh_service(service_name: str, background_tasks: BackgroundTasks):
    """刷新服务状态"""
    try:
        registry = get_registry()
        service = registry.get_service(service_name)
        
        if not service:
            raise HTTPException(status_code=404, detail="服务未找到")
        
        # 后台异步刷新
        background_tasks.add_task(registry._check_service_health, service_name)
        
        return {"message": f"正在刷新服务 {service_name}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"刷新服务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/reload")
async def reload_config():
    """重新加载配置文件"""
    try:
        plugin_manager = get_plugin_manager()
        success = await plugin_manager.load_services_from_config()
        
        if success:
            return {"message": "配置已重新加载"}
        else:
            raise HTTPException(status_code=400, detail="重新加载配置失败")
    except Exception as e:
        logger.error(f"重新加载配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/sample")
async def get_sample_config():
    """获取示例配置文件"""
    try:
        plugin_manager = get_plugin_manager()
        sample_config = plugin_manager.create_sample_config()
        return {"config": sample_config}
    except Exception as e:
        logger.error(f"获取示例配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))