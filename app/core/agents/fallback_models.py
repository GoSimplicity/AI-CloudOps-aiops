#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: AI-CloudOps回退模型管理器
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import lru_cache
import hashlib
import logging
import re
import struct
from typing import Any, Dict, List, Optional
import uuid

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ResponseTemplate(Enum):
    """响应模板枚举"""

    DEPLOYMENT = "deployment"
    MONITORING = "monitoring"
    TROUBLESHOOTING = "troubleshooting"
    KUBERNETES = "kubernetes"
    PERFORMANCE = "performance"
    DEFAULT = "default"


class ErrorCode(Enum):
    """错误代码枚举"""

    UNKNOWN = "UNKNOWN"
    INVALID_INPUT = "INVALID_INPUT"
    SESSION_ERROR = "SESSION_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


from app.config.settings import config

DEFAULT_EMBEDDING_DIMENSION = 384
MAX_INPUT_LENGTH = config.rag.max_context_length
MAX_HISTORY_ITEMS = 5
SESSION_ID_MIN_LENGTH = 3
SESSION_ID_MAX_LENGTH = 128

# 编译的正则表达式（性能优化）
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
HARMFUL_CHARS_PATTERN = re.compile(r"[<>{}]")

logger = logging.getLogger("aiops.fallback_models")


@dataclass
class SessionData:
    """会话数据模型"""

    session_id: str
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    history: List[str] = field(default_factory=list)

    def update_activity(self) -> None:
        """更新最后活动时间"""
        self.last_activity = datetime.now()

    def add_to_history(self, item: str) -> None:
        """添加到历史记录，保持最大数量限制"""
        self.history.append(item)
        if len(self.history) > MAX_HISTORY_ITEMS:
            self.history = self.history[-MAX_HISTORY_ITEMS:]
        self.update_activity()


@dataclass
class ResponseContext:
    """响应上下文数据"""

    user_input: str
    session: Optional[SessionData] = None
    additional_context: Optional[Dict[str, Any]] = None


class ResponseTemplateManager:
    """响应模板管理器"""

    def __init__(self):
        self._templates = self._initialize_templates()
        self._keywords = self._initialize_keywords()

    @staticmethod
    def _initialize_templates() -> Dict[ResponseTemplate, str]:
        """初始化响应模板"""
        return {
            ResponseTemplate.DEPLOYMENT: """**部署建议：**

1. **环境准备**
   - 确保系统资源充足
   - 检查网络连接和权限
   - 准备必要的配置文件

2. **部署步骤**
   - 下载或构建应用镜像
   - 配置环境变量和参数
   - 执行部署命令
   - 验证部署状态

3. **注意事项**
   - 遵循最佳实践
   - 做好备份和回滚准备
   - 监控部署过程

> 这是一个通用回答，具体步骤请参考相关技术文档。""",
            ResponseTemplate.MONITORING: """**监控建议：**

1. **基础监控**
   - CPU、内存、磁盘使用率
   - 网络流量和连接数
   - 应用响应时间

2. **告警设置**
   - 设定合理的阈值
   - 配置多级告警
   - 建立通知机制

3. **数据分析**
   - 定期检查监控数据
   - 分析趋势和异常
   - 优化监控策略

> 建议使用专业的监控工具如Prometheus。""",
            ResponseTemplate.TROUBLESHOOTING: """**故障排除指南：**

1. **问题定位**
   - 收集错误信息和日志
   - 确定问题发生时间和范围
   - 检查相关组件状态

2. **诊断步骤**
   - 检查系统资源使用情况
   - 验证配置和权限
   - 测试网络连接
   - 查看应用日志

3. **解决方案**
   - 重启相关服务
   - 修复配置问题
   - 更新或回滚版本
   - 扩展系统资源

> 建议建立完善的故障处理流程和文档。""",
            ResponseTemplate.KUBERNETES: """**Kubernetes 运维建议：**

1. **基本操作**
   ```bash
   kubectl get pods
   kubectl describe pod <pod-name>
   kubectl logs <pod-name>
   ```

2. **常见问题**
   - Pod状态异常：检查资源配额和节点状态
   - 镜像拉取失败：验证镜像名称和网络
   - 配置错误：检查YAML文件格式

3. **最佳实践**
   - 使用资源限制和请求
   - 配置健康检查
   - 实施滚动更新策略

> 详细操作请参考Kubernetes官方文档。""",
            ResponseTemplate.PERFORMANCE: """**性能优化建议：**

1. **系统层面**
   - 监控CPU、内存、磁盘I/O
   - 优化网络配置
   - 调整系统参数

2. **应用层面**
   - 代码优化和重构
   - 数据库查询优化
   - 缓存策略实施

3. **架构层面**
   - 负载均衡配置
   - 微服务拆分
   - 容器资源调优

> 建议进行性能测试和监控分析。""",
        }

    @staticmethod
    def _initialize_keywords() -> Dict[ResponseTemplate, List[str]]:
        """初始化关键词映射"""
        return {
            ResponseTemplate.DEPLOYMENT: ["部署", "安装", "配置"],
            ResponseTemplate.MONITORING: ["监控", "告警", "指标"],
            ResponseTemplate.TROUBLESHOOTING: ["故障", "问题", "错误", "异常"],
            ResponseTemplate.KUBERNETES: ["kubernetes", "k8s", "pod", "deployment"],
            ResponseTemplate.PERFORMANCE: ["性能", "优化", "慢", "卡顿"],
        }

    def get_template_type(self, user_input: str) -> ResponseTemplate:
        """根据用户输入确定模板类型"""
        user_input_lower = user_input.lower()

        for template_type, keywords in self._keywords.items():
            if any(keyword in user_input_lower for keyword in keywords):
                return template_type

        return ResponseTemplate.DEFAULT

    def get_response(
        self, template_type: ResponseTemplate, user_input: str = ""
    ) -> str:
        """获取响应内容"""
        if template_type == ResponseTemplate.DEFAULT:
            return self._generate_default_response(user_input)

        return self._templates.get(
            template_type, self._generate_default_response(user_input)
        )

    def _generate_default_response(self, user_input: str) -> str:
        """生成默认响应"""
        return f"""感谢您的问题："{user_input}"

很抱歉，当前AI服务暂时不可用，我只能提供基础的技术建议：

**一般性运维建议：**
1. **监控为先** - 建立完善的监控体系
2. **自动化优先** - 减少手动操作，提高效率
3. **文档齐全** - 维护详细的操作文档
4. **备份策略** - 定期备份重要数据和配置
5. **安全第一** - 实施安全最佳实践

**获取更多帮助：**
- 查阅官方技术文档
- 联系技术支持团队
- 参考社区最佳实践

> 这是一个临时回答，建议您稍后重试或联系技术支持获得更准确的帮助。"""


class FallbackEmbeddings(Embeddings):
    """优化的备用嵌入实现 - 当主要嵌入服务不可用时使用"""

    def __init__(
        self, dimension: int = DEFAULT_EMBEDDING_DIMENSION, enable_cache: bool = True
    ):
        self.dimension = dimension
        self.enable_cache = enable_cache
        if enable_cache:
            # 使用LRU缓存提高性能
            self._generate_pseudo_embedding = lru_cache(maxsize=1000)(
                self._generate_pseudo_embedding_impl
            )
        else:
            self._generate_pseudo_embedding = self._generate_pseudo_embedding_impl

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """为文档列表生成嵌入向量"""
        if not texts:
            return []

        logger.warning(f"使用备用嵌入实现处理 {len(texts)} 个文档")

        try:
            embeddings = [self._generate_pseudo_embedding(text) for text in texts]
            return embeddings
        except Exception as e:
            logger.error("生成文档嵌入时出错，异常类型: %s", type(e).__name__)
            # 返回零向量作为降级方案
            return [[0.0] * self.dimension for _ in texts]

    def embed_query(self, text: str) -> List[float]:
        """为查询生成嵌入向量"""
        logger.warning("使用备用查询嵌入实现")

        try:
            return self._generate_pseudo_embedding(text)
        except Exception as e:
            logger.error("生成查询嵌入时出错，异常类型: %s", type(e).__name__)
            return [0.0] * self.dimension

    def _generate_pseudo_embedding_impl(self, text: str) -> List[float]:
        """生成伪嵌入向量的实际实现"""
        if not text:
            return [0.0] * self.dimension

        # 使用多种哈希算法增加向量多样性
        hash_funcs = [hashlib.md5, hashlib.sha1, hashlib.sha256]
        embedding = []

        for hash_func in hash_funcs:
            hash_obj = hash_func(text.encode("utf-8"))
            hash_bytes = hash_obj.digest()

            # 将哈希转换为浮点数
            for i in range(0, len(hash_bytes), 4):
                if len(embedding) >= self.dimension:
                    break

                chunk = hash_bytes[i : i + 4]
                if len(chunk) == 4:
                    try:
                        float_val = struct.unpack("f", chunk)[0]
                        # 标准化到 [-1, 1] 范围
                        if not (float_val != float_val):  # 检查NaN
                            embedding.append(max(-1.0, min(1.0, float_val)))
                    except struct.error:
                        embedding.append(0.0)

            if len(embedding) >= self.dimension:
                break

        # 确保向量长度正确
        while len(embedding) < self.dimension:
            embedding.append(0.0)

        return embedding[: self.dimension]


class FallbackChatModel(BaseChatModel):
    """优化的备用聊天模型 - 当主要LLM服务不可用时使用"""

    model_name: str = "fallback-model"
    template_manager: ResponseTemplateManager = None

    def __init__(
        self, template_manager: Optional[ResponseTemplateManager] = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.template_manager = template_manager or ResponseTemplateManager()

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """生成聊天响应"""
        logger.warning("使用备用聊天模型")

        try:
            # 提取用户消息
            user_message = self._extract_user_message(messages)

            # 生成响应
            response_content = self._generate_structured_response(user_message)

            generation = ChatGeneration(
                message=type(messages[-1])(content=response_content),
                generation_info={
                    "model": self.model_name,
                    "timestamp": datetime.now().isoformat(),
                    "fallback": True,
                },
            )

            return ChatResult(generations=[generation])

        except Exception as e:
            logger.error("备用聊天模型生成响应时出错，异常类型: %s", type(e).__name__)
            # 返回通用错误响应
            error_response = "抱歉，当前服务不可用，请稍后重试。"
            generation = ChatGeneration(
                message=type(messages[-1])(content=error_response),
                generation_info={"model": self.model_name, "error": "生成响应失败"},
            )
            return ChatResult(generations=[generation])

    def _extract_user_message(self, messages: List[BaseMessage]) -> str:
        """提取用户消息"""
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.content:
                return str(msg.content).strip()
        return ""

    def _generate_structured_response(self, user_input: str) -> str:
        """生成结构化响应"""
        if not user_input:
            return "请提供您的问题或需求。"

        # 清理输入
        cleaned_input = sanitize_input(user_input)

        # 确定响应模板类型
        template_type = self.template_manager.get_template_type(cleaned_input)

        # 生成响应
        return self.template_manager.get_response(template_type, cleaned_input)

    @property
    def _llm_type(self) -> str:
        return "fallback-chat"


def sanitize_input(text: str, max_length: int = MAX_INPUT_LENGTH) -> str:
    """优化的输入清理和验证函数"""
    if not text:
        return ""

    # 使用预编译的正则表达式提高性能
    cleaned = HARMFUL_CHARS_PATTERN.sub("", text)

    # 限制长度
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + "..."

    return cleaned.strip()


def validate_session_id(session_id: str) -> bool:
    """优化的会话ID验证函数"""
    if not session_id:
        return False

    # 长度检查
    if not (SESSION_ID_MIN_LENGTH <= len(session_id) <= SESSION_ID_MAX_LENGTH):
        return False

    try:
        # 尝试解析为UUID（最常见的情况）
        uuid.UUID(session_id)
        return True
    except ValueError:
        # 如果不是UUID格式，使用预编译的正则表达式检查
        return bool(SESSION_ID_PATTERN.match(session_id))


@lru_cache(maxsize=100)
def create_session_id() -> str:
    """创建新的会话ID（带缓存优化）"""
    return str(uuid.uuid4())


def build_context_with_history(session: Optional[SessionData]) -> Optional[str]:
    """优化的历史上下文构建函数"""
    if not session or not session.history:
        return None

    # 获取最近的对话历史
    recent_history = session.history[-3:]  # 最近3轮对话

    if not recent_history:
        return None

    # 使用列表推导式和join提高性能
    context_parts = ["## 对话历史"] + [
        f"{i}. {item}" for i, item in enumerate(recent_history, 1)
    ]

    return "\n".join(context_parts)


def format_error_response(
    error_message: str,
    error_code: ErrorCode = ErrorCode.UNKNOWN,
    additional_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """优化的错误响应格式化函数"""
    response = {
        "error": True,
        "error_code": error_code.value,
        "error_message": error_message,
        "timestamp": datetime.now().isoformat(),
        "suggestion": "请检查输入参数或稍后重试",
    }

    if additional_info:
        response.update(additional_info)

    return response


def generate_fallback_answer(context: Optional[ResponseContext] = None) -> str:
    """生成智能降级答案"""
    base_message = """很抱歉，AI助手服务当前不可用。

**可能的原因：**
- 服务正在维护中
- 网络连接问题
- 系统负载过高

**建议操作：**
1. 稍后重试
2. 检查网络连接
3. 联系技术支持

**临时解决方案：**
- 查阅技术文档
- 参考历史解决案例
- 咨询团队成员

感谢您的理解！"""

    # 如果有上下文信息，可以提供更个性化的建议
    if context and context.session and context.session.history:
        base_message += f"\n\n**基于您的历史记录：**\n- 最近关注: {', '.join(context.session.history[-2:])}"

    return base_message


class SessionManager:
    """会话管理器"""

    def __init__(self):
        self._sessions: Dict[str, SessionData] = {}

    def get_session(self, session_id: str) -> Optional[SessionData]:
        """获取会话"""
        if not validate_session_id(session_id):
            return None
        return self._sessions.get(session_id)

    def create_session(self, session_id: Optional[str] = None) -> SessionData:
        """创建新会话"""
        if not session_id:
            session_id = create_session_id()

        session = SessionData(session_id=session_id)
        self._sessions[session_id] = session
        return session

    def update_session(self, session_id: str, item: str) -> bool:
        """更新会话历史"""
        session = self.get_session(session_id)
        if session:
            session.add_to_history(item)
            return True
        return False

    def cleanup_expired_sessions(self, max_age_hours: int = 24) -> int:
        """清理过期会话"""
        from datetime import timedelta

        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        expired_sessions = [
            sid
            for sid, session in self._sessions.items()
            if session.last_activity < cutoff_time
        ]

        for sid in expired_sessions:
            del self._sessions[sid]

        return len(expired_sessions)


__all__ = [
    "SessionData",
    "ResponseContext",
    "FallbackEmbeddings",
    "FallbackChatModel",
    "SessionManager",
    "ResponseTemplateManager",
    "ErrorCode",
    "sanitize_input",
    "validate_session_id",
    "create_session_id",
    "build_context_with_history",
    "format_error_response",
    "generate_fallback_answer",
]
