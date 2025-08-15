#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis向量存储实现
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: 脚本：ensure_py_headers
"""

from __future__ import annotations

import os
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def compute_description(p: Path, content: str) -> str:
    path = str(p.as_posix())
    name = p.stem.lower()
    # app/api/routes
    if "/app/api/routes/" in path:
        if name == "rca":
            return "RCA 根因分析 API 路由"
        if name == "health":
            return "健康检查 API 路由"
        if name == "assistant":
            return "助手/问答 API 路由"
        if name == "predict":
            return "预测服务 API 路由"
        if name == "autofix":
            return "Autofix 自动修复 API 路由"
        if name == "multi_agent":
            return "多Agent 编排 API 路由"
        return f"API 路由（{p.stem}）"
    # app/api/middleware
    if "/app/api/middleware/" in path:
        if name == "cors":
            return "API 中间件（CORS 跨域）"
        if name == "error_handler":
            return "API 中间件（统一错误处理）"
        return "API 中间件"
    # config
    if "/app/config/" in path:
        if name == "settings":
            return "应用配置加载与环境变量解析"
        if name == "logging":
            return "日志系统配置"
        return "应用配置模块"
    # db
    if "/app/db/" in path:
        if name == "base":
            return "数据库引擎与会话管理"
        if name == "models":
            return "数据库模型定义"
        if name == "init":
            return "数据库初始化/建表"
        return "数据库模块"
    # services
    if "/app/services/" in path:
        if name == "prometheus":
            return "Prometheus 客户端"
        if name == "kubernetes":
            return "Kubernetes 客户端"
        if name == "llm":
            return "LLM 服务封装"
        if name == "notification":
            return "通知服务（飞书等）"
        if name == "tracing":
            return "Trace/Jaeger 服务封装"
        return f"服务层（{p.stem}）"
    # core/rca
    if "/app/core/rca/" in path:
        if name == "analyzer":
            return "RCA 根因分析核心"
        if name == "correlator":
            return "RCA 相关性与跨时滞分析"
        if name == "detector":
            return "RCA 异常检测器"
        if "collectors/" in path:
            if name == "k8s_events_collector":
                return "K8s 事件采集器"
            if name == "k8s_state_collector":
                return "K8s 状态快照采集器"
            if name == "logs_collector":
                return "容器日志采集器"
            if name == "tracing_collector":
                return "Trace 采集器"
            return "RCA 数据采集器"
        if "jobs/" in path:
            if name == "job_manager":
                return "RCA 任务管理器（Huey）"
            if name == "tasks":
                return "RCA 异步任务定义（Huey）"
            if name == "huey_app":
                return "Huey 实例（RCA 队列）"
        if "topology/" in path:
            return "RCA 拓扑关系构建"
        if "rules/" in path:
            return "RCA 规则引擎"
        if "explainer/" in path:
            return "RCA 说明/格式化（占位）"
        return f"RCA 子模块（{p.stem}）"
    # core/agents / others
    if "/app/core/agents/" in path:
        return f"多Agent 模块（{p.stem}）"
    if "/app/core/prediction/" in path:
        return f"预测服务（{p.stem}）"
    if "/app/core/vector/" in path:
        return f"向量存储（{p.stem}）"
    if "/app/core/cache/" in path:
        return f"缓存管理（{p.stem}）"
    # models
    if "/app/models/" in path:
        if name == "request_models":
            return "Pydantic 请求模型"
        if name == "response_models":
            return "Pydantic 响应模型"
        if name == "data_models":
            return "数据模型（内部）"
        if name == "entities":
            return "Pydantic 实体模型"
        return f"Pydantic 模型（{p.stem}）"
    # utils
    if "/app/utils/" in path:
        if name == "time_utils":
            return "时间工具"
        if name == "validators":
            return "输入校验工具"
        if name == "pagination":
            return "分页工具"
        if name == "error_handlers":
            return "通用错误处理工具"
        return f"通用工具（{p.stem}）"
    # mcp
    if "/app/mcp/server/" in path:
        if "/tools/" in path:
            return f"MCP 工具（{p.stem}）"
        if name == "mcp_server":
            return "MCP 服务端"
        if name == "main":
            return "MCP 服务入口"
        return f"MCP 服务模块（{p.stem}）"
    if "/app/mcp/" in path:
        if name == "mcp_client":
            return "MCP 客户端"
        return f"MCP 模块（{p.stem}）"
    # app root
    if "/app/main.py" in path:
        return "FastAPI 应用入口"
    if "/app/di.py" in path:
        return "依赖注入注册"
    # tests
    if "/tests/" in path:
        return f"测试：{p.stem}"
    # scripts
    if "/scripts/" in path:
        return f"脚本：{p.stem}"
    # default
    return f"模块：{p.stem}"


def build_header_for(path: Path, content: str) -> str:
    desc = compute_description(path, content)
    return (
        "#!/usr/bin/env python3\n"
        "# -*- coding: utf-8 -*-\n"
        '"""\n'
        "Redis向量存储实现\n"
        "Author: Bamboo\n"
        "Email: bamboocloudops@gmail.com\n"
        "License: Apache 2.0\n"
        f"Description: {desc}\n"
        '"""\n\n'
    )


HEADER_PATTERN = re.compile(
    r"^(?:\ufeff)?"  # 可选 BOM
    r"(?:\#\!.*\n)?"  # 可选 shebang
    r"(?:\#.*coding[:=].*\n)?"  # 可选编码行
    r'(?:"""[\s\S]*?"""\n)?',  # 可选顶层docstring
    re.MULTILINE,
)


EXCLUDE_DIRS = {
    ".git",
    "venv",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def ensure_header_for_file(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return False

    content_norm = normalize_newlines(content)
    desired_header = build_header_for(path, content_norm)
    if content_norm.startswith(desired_header):
        return False

    # 去掉已有头部（shebang/编码/顶层docstring）
    rest = HEADER_PATTERN.sub("", content_norm, count=1)

    # 去除文件开头多余空行
    rest = rest.lstrip("\n")

    new_content = desired_header + rest
    if new_content != content_norm:
        path.write_text(new_content, encoding="utf-8")
        return True
    return False


def main() -> None:
    changed = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        # 跳过不必要目录
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        for name in filenames:
            if not name.endswith(".py"):
                continue
            p = Path(dirpath) / name
            total += 1
            if ensure_header_for_file(p):
                changed += 1

    print(f"Processed: {total}, Updated: {changed}")


if __name__ == "__main__":
    main()

