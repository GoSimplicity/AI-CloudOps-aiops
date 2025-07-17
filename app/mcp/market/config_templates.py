"""
MCP配置模板管理
提供标准的服务配置模板和验证功能
"""

import os
import json
import yaml
from typing import Dict, List, Any
from pathlib import Path


class ConfigTemplateManager:
    """MCP配置模板管理器"""
    
    # 标准工具服务模板
    TOOL_TEMPLATES = {
        "time_service": {
            "name": "time_service",
            "display_name": "时间服务",
            "description": "提供时间相关的工具服务，包括获取当前时间、时区转换等",
            "version": "1.0.0",
            "server_url": "http://localhost:9000",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 1,
            "tags": ["utility", "time", "basic"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "utility",
                "tools": ["get_current_time", "get_timezone", "convert_time"]
            }
        },
        
        "file_service": {
            "name": "file_service",
            "display_name": "文件服务",
            "description": "提供文件系统操作相关的工具服务，包括读写文件、目录操作等",
            "version": "1.0.0",
            "server_url": "http://localhost:9001",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 2,
            "tags": ["file", "system", "io"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "system",
                "tools": ["read_file", "write_file", "list_directory", "create_directory"]
            }
        },
        
        "calculator_service": {
            "name": "calculator_service",
            "display_name": "计算器服务",
            "description": "提供数学计算相关的工具服务，包括基本运算、统计分析等",
            "version": "1.0.0",
            "server_url": "http://localhost:9002",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 1,
            "tags": ["math", "calculator", "utility"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "utility",
                "tools": ["calculate", "solve_equation", "statistics"]
            }
        },
        
        "system_info_service": {
            "name": "system_info_service",
            "display_name": "系统信息服务",
            "description": "提供系统信息查询相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9003",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 1,
            "tags": ["system", "info", "monitoring"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "system",
                "tools": ["get_system_info", "get_process_info", "get_disk_usage"]
            }
        },
        
        "web_service": {
            "name": "web_service",
            "display_name": "网络服务",
            "description": "提供网络请求相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9004",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 2,
            "tags": ["web", "http", "network"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "network",
                "tools": ["http_request", "fetch_url", "parse_html"]
            }
        },
        
        "database_service": {
            "name": "database_service",
            "display_name": "数据库服务",
            "description": "提供数据库操作相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9005",
            "timeout": 30,
            "max_retries": 3,
            "health_check_interval": 60,
            "weight": 2,
            "tags": ["database", "sql", "data"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "database",
                "tools": ["execute_sql", "query_data", "backup_database"]
            }
        }
    }
    
    # 高级服务模板
    ADVANCED_TEMPLATES = {
        "kubernetes_service": {
            "name": "kubernetes_service",
            "display_name": "Kubernetes服务",
            "description": "提供Kubernetes集群管理相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9100",
            "timeout": 60,
            "max_retries": 2,
            "health_check_interval": 120,
            "weight": 3,
            "tags": ["kubernetes", "k8s", "cluster", "devops"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "devops",
                "tools": ["get_pods", "get_services", "get_deployments", "apply_yaml"]
            }
        },
        
        "monitoring_service": {
            "name": "monitoring_service",
            "display_name": "监控服务",
            "description": "提供系统监控和告警相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9101",
            "timeout": 45,
            "max_retries": 2,
            "health_check_interval": 90,
            "weight": 2,
            "tags": ["monitoring", "metrics", "alerting", "observability"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "monitoring",
                "tools": ["get_metrics", "check_health", "send_alert"]
            }
        },
        
        "ai_service": {
            "name": "ai_service",
            "display_name": "AI服务",
            "description": "提供AI模型调用相关的工具服务",
            "version": "1.0.0",
            "server_url": "http://localhost:9102",
            "timeout": 120,
            "max_retries": 1,
            "health_check_interval": 180,
            "weight": 3,
            "tags": ["ai", "ml", "model", "inference"],
            "metadata": {
                "author": "AI-CloudOps",
                "category": "ai",
                "tools": ["text_generation", "image_classification", "sentiment_analysis"]
            }
        }
    }
    
    @classmethod
    def get_all_templates(cls) -> Dict[str, Dict[str, Any]]:
        """获取所有模板"""
        return {**cls.TOOL_TEMPLATES, **cls.ADVANCED_TEMPLATES}
    
    @classmethod
    def get_template(cls, template_name: str) -> Optional[Dict[str, Any]]:
        """获取指定模板"""
        all_templates = cls.get_all_templates()
        return all_templates.get(template_name)
    
    @classmethod
    def get_templates_by_category(cls, category: str) -> Dict[str, Dict[str, Any]]:
        """按类别获取模板"""
        templates = cls.get_all_templates()
        return {
            name: template for name, template in templates.items()
            if template.get("metadata", {}).get("category") == category
        }
    
    @classmethod
    def get_template_categories(cls) -> List[str]:
        """获取所有模板类别"""
        templates = cls.get_all_templates()
        categories = set()
        for template in templates.values():
            category = template.get("metadata", {}).get("category")
            if category:
                categories.add(category)
        return sorted(list(categories))
    
    @classmethod
    def create_config_from_template(
        cls, 
        template_name: str, 
        server_url: str, 
        custom_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """从模板创建配置"""
        template = cls.get_template(template_name)
        if not template:
            raise ValueError(f"模板 {template_name} 不存在")
        
        config = template.copy()
        config["server_url"] = server_url
        
        if custom_config:
            config.update(custom_config)
        
        return config
    
    @classmethod
    def create_full_config(
        cls, 
        services: List[Dict[str, Any]], 
        global_settings: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """创建完整配置文件"""
        config = {
            "services": services,
            "global_timeout": 30,
            "max_concurrent_requests": 100,
            "health_check_enabled": True,
            "load_balancing": "weighted_round_robin",
            "cache_enabled": True,
            "cache_ttl": 300
        }
        
        if global_settings:
            config.update(global_settings)
        
        return config
    
    @classmethod
    def generate_development_config(cls) -> Dict[str, Any]:
        """生成开发环境配置"""
        services = [
            cls.create_config_from_template("time_service", "http://localhost:9000"),
            cls.create_config_from_template("file_service", "http://localhost:9001"),
            cls.create_config_from_template("calculator_service", "http://localhost:9002"),
            cls.create_config_from_template("system_info_service", "http://localhost:9003"),
        ]
        
        return cls.create_full_config(services, {
            "global_timeout": 30,
            "max_concurrent_requests": 50,
            "environment": "development"
        })
    
    @classmethod
    def generate_production_config(cls) -> Dict[str, Any]:
        """生成生产环境配置"""
        services = [
            cls.create_config_from_template("time_service", "http://mcp-time:9000"),
            cls.create_config_from_template("file_service", "http://mcp-file:9001"),
            cls.create_config_from_template("calculator_service", "http://mcp-calculator:9002"),
            cls.create_config_from_template("system_info_service", "http://mcp-system:9003"),
            cls.create_config_from_template("web_service", "http://mcp-web:9004"),
            cls.create_config_from_template("database_service", "http://mcp-db:9005"),
            cls.create_config_from_template("kubernetes_service", "http://mcp-k8s:9100"),
            cls.create_config_from_template("monitoring_service", "http://mcp-monitoring:9101"),
        ]
        
        return cls.create_full_config(services, {
            "global_timeout": 60,
            "max_concurrent_requests": 200,
            "environment": "production",
            "health_check_enabled": True
        })
    
    @classmethod
    def save_config(cls, config: Dict[str, Any], file_path: str, format: str = "yaml"):
        """保存配置到文件"""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        if format.lower() == "json":
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        else:
            with open(file_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, indent=2, allow_unicode=True)
    
    @classmethod
    def load_config(cls, file_path: str) -> Dict[str, Any]:
        """从文件加载配置"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {file_path}")
        
        with open(file_path, "r", encoding="utf-8") as f:
            if file_path.endswith(".json"):
                return json.load(f)
            else:
                return yaml.safe_load(f)
    
    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> List[str]:
        """验证配置有效性"""
        errors = []
        
        if "services" not in config:
            errors.append("配置缺少 'services' 字段")
            return errors
        
        services = config.get("services", [])
        for i, service in enumerate(services):
            prefix = f"服务[{i}]"
            
            if not isinstance(service, dict):
                errors.append(f"{prefix} 必须是字典类型")
                continue
            
            required_fields = ["name", "server_url"]
            for field in required_fields:
                if field not in service:
                    errors.append(f"{prefix} 缺少必需字段: {field}")
            
            if "server_url" in service:
                url = service["server_url"]
                if not url.startswith(("http://", "https://")):
                    errors.append(f"{prefix} server_url 格式无效: {url}")
            
            if "timeout" in service and (not isinstance(service["timeout"], int) or service["timeout"] <= 0):
                errors.append(f"{prefix} timeout 必须是正整数")
        
        return errors
    
    @classmethod
    def create_sample_configs(cls, output_dir: str):
        """创建示例配置文件"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # 开发配置
        dev_config = cls.generate_development_config()
        cls.save_config(dev_config, output_path / "mcp_services.development.yaml")
        cls.save_config(dev_config, output_path / "mcp_services.development.json", format="json")
        
        # 生产配置
        prod_config = cls.generate_production_config()
        cls.save_config(prod_config, output_path / "mcp_services.production.yaml")
        cls.save_config(prod_config, output_path / "mcp_services.production.json", format="json")
        
        # 最小配置
        minimal_config = {
            "services": [
                cls.create_config_from_template("time_service", "http://localhost:9000"),
                cls.create_config_from_template("file_service", "http://localhost:9001"),
            ],
            "global_timeout": 30
        }
        cls.save_config(minimal_config, output_path / "mcp_services.minimal.yaml")
        
        # 创建README
        readme_content = """# MCP服务配置

## 文件说明

- `mcp_services.development.yaml` - 开发环境配置
- `mcp_services.production.yaml` - 生产环境配置  
- `mcp_services.minimal.yaml` - 最小化配置

## 使用方式

1. 复制合适的配置文件到 `config/mcp_services.yaml`
2. 根据需要修改服务地址和参数
3. 重启应用使配置生效

## 模板列表

可用模板：{templates}

## 自定义服务

可以通过API或手动编辑配置文件添加自定义服务。
""".format(templates=", ".join(cls.get_all_templates().keys()))
        
        with open(output_path / "README.md", "w", encoding="utf-8") as f:
            f.write(readme_content)