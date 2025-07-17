"""
MCP市场路由
提供MCP服务的RESTful API接口
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Any, Optional
import logging

from .models import MCPService, ToolResponse
from .client_v2 import EnhancedMCPClient, MCPServiceManager

logger = logging.getLogger("aiops.mcp.router")

router = APIRouter(prefix="/mcp/market", tags=["MCP市场"])

async def get_mcp_client() -> EnhancedMCPClient:
    """获取MCP客户端实例"""
    return await MCPServiceManager.get_instance()


@router.get("/status", response_model=Dict[str, Any])
async def get_market_status(
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """获取MCP市场状态"""
    try:
        return await client.get_market_status()
    except Exception as e:
        logger.error(f"获取MCP市场状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/services", response_model=List[Dict[str, Any]])
async def list_services(
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> List[Dict[str, Any]]:
    """获取所有MCP服务"""
    try:
        services = await client.list_services()
        return [service.to_dict() for service in services]
    except Exception as e:
        logger.error(f"获取服务列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/services", response_model=Dict[str, Any])
async def add_service(
    service: Dict[str, Any],
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """添加MCP服务"""
    try:
        from .models import MCPService
        service_obj = MCPService(**service)
        success = await client.add_service(service_obj)
        return {"success": success, "service": service_obj.to_dict()}
    except Exception as e:
        logger.error(f"添加服务失败: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/services/{service_name}", response_model=Dict[str, Any])
async def remove_service(
    service_name: str,
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """移除MCP服务"""
    try:
        success = await client.remove_service(service_name)
        return {"success": success, "service_name": service_name}
    except Exception as e:
        logger.error(f"移除服务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/services/{service_name}", response_model=Dict[str, Any])
async def get_service(
    service_name: str,
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """获取单个服务详情"""
    try:
        service = await client.get_service(service_name)
        if not service:
            raise HTTPException(status_code=404, detail="服务未找到")
        return service.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取服务详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools", response_model=Dict[str, List[Dict[str, Any]]])
async def get_available_tools(
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, List[Dict[str, Any]]]:
    """获取所有可用工具"""
    try:
        return await client.get_available_tools()
    except Exception as e:
        logger.error(f"获取工具列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tools/execute", response_model=Dict[str, Any])
async def execute_tool(
    request: Dict[str, Any],
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """执行工具调用"""
    try:
        tool_name = request.get("tool_name")
        parameters = request.get("parameters", {})
        preferred_services = request.get("preferred_services")
        
        if not tool_name:
            raise HTTPException(status_code=400, detail="缺少tool_name参数")
        
        result = await client.execute_tool(
            tool_name=tool_name,
            parameters=parameters,
            preferred_services=preferred_services
        )
        
        return {
            "success": result.success,
            "result": result.result,
            "service_name": result.service_name,
            "response_time": result.response_time,
            "error": result.error
        }
    except Exception as e:
        logger.error(f"执行工具失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tools/batch_execute", response_model=List[Dict[str, Any]])
async def batch_execute_tools(
    requests: List[Dict[str, Any]],
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> List[Dict[str, Any]]:
    """批量执行工具调用"""
    try:
        results = await client.batch_execute_tools(requests)
        return [
            {
                "success": result.success,
                "result": result.result,
                "service_name": result.service_name,
                "response_time": result.response_time,
                "error": result.error
            }
            for result in results
        ]
    except Exception as e:
        logger.error(f"批量执行工具失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/discover", response_model=Dict[str, Any])
async def discover_services(
    request: Dict[str, Any],
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """发现并注册新的MCP服务"""
    try:
        service_urls = request.get("service_urls", [])
        if not service_urls:
            raise HTTPException(status_code=400, detail="缺少service_urls参数")
        
        results = await client.discover_services(service_urls)
        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"发现服务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health", response_model=Dict[str, Any])
async def health_check(
    client: EnhancedMCPClient = Depends(get_mcp_client)
) -> Dict[str, Any]:
    """健康检查"""
    try:
        status = await client.get_market_status()
        return {
            "status": "healthy" if status.get("total_services", 0) > 0 else "unhealthy",
            "services": status
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return {"status": "unhealthy", "error": str(e)}