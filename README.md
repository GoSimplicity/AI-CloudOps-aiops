# AI-CloudOps 项目文档（AIOPS部分）

## 📋 项目概述

AI-CloudOps 是一个基于人工智能的云原生运维平台，提供智能化的运维管理、故障分析和自动化修复功能。该项目结合了机器学习、大语言模型和云原生技术，为现代化的运维管理提供全面的解决方案。

### 🎯 核心功能

1. **智能负载预测** - 基于历史数据和机器学习模型预测系统负载
2. **根因分析（RCA）** - 自动分析系统故障和性能问题的根本原因
3. **自动化修复** - 智能化的Kubernetes资源自动修复和优化
4. **智能小助手** - 基于RAG技术的运维知识问答和建议系统
5. **健康检查** - 全面的系统健康监控和状态报告
6. **实时监控** - 与Prometheus集成的实时监控和告警

### 🏗️ 技术架构

```
AI-CloudOps 架构图
┌─────────────────────────────────────────────────────────────┐
│                     前端接口层                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │   REST API  │  │  WebSocket  │  │   GraphQL   │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
├─────────────────────────────────────────────────────────────┤
│                     业务逻辑层                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │   预测服务   │  │   分析服务   │  │   修复服务   │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │   助手服务   │  │   监控服务   │  │   通知服务   │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
├─────────────────────────────────────────────────────────────┤
│                     数据存储层                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │  向量数据库  │  │  时序数据库  │  │  关系数据库  │          │
│  │  (ChromaDB) │  │(Prometheus) │  │  (可选)     │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
├─────────────────────────────────────────────────────────────┤
│                     基础设施层                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │ Kubernetes  │  │    Docker   │  │   云平台     │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
└─────────────────────────────────────────────────────────────┘
```

## 📁 项目结构

```
AI-CloudOps-backend/python/
├── app/                          # 应用主目录
│   ├── __init__.py              # 应用初始化
│   ├── main.py                  # 应用入口文件
│   ├── constants.py             # 系统常量定义
│   ├── api/                     # API接口层
│   │   ├── __init__.py
│   │   ├── middleware/          # 中间件
│   │   │   ├── cors.py          # CORS处理
│   │   │   └── error_handler.py # 错误处理
│   │   └── routes/              # API路由
│   │       ├── assistant.py     # 智能助手API
│   │       ├── autofix.py       # 自动修复API
│   │       ├── health.py        # 健康检查API
│   │       ├── predict.py       # 负载预测API
│   │       └── rca.py           # 根因分析API
│   ├── config/                  # 配置管理
│   │   ├── __init__.py
│   │   ├── logging.py           # 日志配置
│   │   └── settings.py          # 应用设置
│   ├── core/                    # 核心业务逻辑
│   │   ├── agents/              # AI代理系统
│   │   │   ├── assistant.py     # RAG智能助手
│   │   │   ├── coder.py         # 代码生成代理
│   │   │   ├── k8s_fixer.py     # K8s自动修复代理
│   │   │   ├── notifier.py      # 通知代理
│   │   │   ├── researcher.py    # 研究分析代理
│   │   │   └── supervisor.py    # 主管代理
│   │   ├── prediction/          # 预测模块
│   │   │   ├── model_loader.py  # 模型加载器
│   │   │   └── predictor.py     # 预测服务
│   │   └── rca/                 # 根因分析模块
│   │       ├── analyzer.py      # 主分析器
│   │       ├── correlator.py    # 相关性分析
│   │       └── detector.py      # 异常检测
│   ├── models/                  # 数据模型
│   │   ├── data_models.py       # 数据模型定义
│   │   ├── request_models.py    # 请求模型
│   │   └── response_models.py   # 响应模型
│   ├── services/                # 服务层
│   │   ├── kubernetes.py        # K8s服务
│   │   ├── llm.py               # LLM服务
│   │   ├── notification.py      # 通知服务
│   │   └── prometheus.py        # 监控服务
│   └── utils/                   # 工具类
│       ├── error_handlers.py    # 错误处理工具
│       ├── metrics.py           # 指标工具
│       ├── time_utils.py        # 时间工具
│       └── validators.py        # 验证工具
├── config/                      # 配置文件
│   ├── config.yaml              # 主配置文件
│   └── config.production.yaml   # 生产环境配置
├── data/                        # 数据目录
│   ├── models/                  # 机器学习模型
│   ├── knowledge_base/          # 知识库
│   ├── vector_db/               # 向量数据库
│   └── sample/                  # 示例数据
├── deploy/                      # 部署相关
│   ├── kubernetes/              # K8s部署文件
│   ├── grafana/                 # Grafana配置
│   └── prometheus/              # Prometheus配置
├── docs/                        # 文档目录
├── tests/                       # 测试文件
├── scripts/                     # 脚本文件
├── requirements.txt             # Python依赖
├── docker-compose.yml           # Docker编排
└── Dockerfile                   # Docker镜像
```

## 🚀 快速开始

### 环境要求

- Python 3.8+
- Docker & Docker Compose
- Kubernetes (可选)
- Prometheus (监控)

### 安装步骤

1. **克隆项目**
```bash
git clone 'https://github.com/GoSimplicity/AI-CloudOps.git'
cd AI-CloudOps-backend/python
```

2. **安装依赖**
```bash
pip install -r requirements.txt
```

3. **配置环境变量**
```bash
cp env.example .env
# 编辑 .env 文件，配置必要的环境变量
```

4. **启动服务**
```bash
# 开发环境（从项目根目录执行）
python -m app.main

# 或使用启动脚本
bash scripts/start.sh
```

### 配置说明

主要配置文件：`config/config.yaml`

## 📊 核心模块详解

### 1. 智能负载预测 (Prediction)

**位置**: `app/core/prediction/`

**功能**:
- 基于历史QPS数据预测未来负载
- 支持多种时间窗口预测
- 智能实例数量建议
- 置信度评估

**主要算法**:
- 时间序列分析
- 线性回归模型
- 周期性模式识别
- 趋势分析

**使用示例**:
```python
from app.core.prediction.predictor import PredictionService

predictor = PredictionService()
result = await predictor.predict_load(
    current_qps=100,
    hours_ahead=24,
    confidence_level=0.95
)
```

### 2. 根因分析 (RCA)

**位置**: `app/core/rca/`

**功能**:
- 自动化的系统故障分析
- 多维度相关性分析
- 异常检测和模式识别
- 智能化的根因推荐

**主要算法**:
- 统计异常检测
- 相关性分析
- 图论分析
- 机器学习分类

**使用示例**:
```python
from app.core.rca.analyzer import RCAAnalyzer

analyzer = RCAAnalyzer()
result = await analyzer.analyze_issue(
    metrics=metrics_data,
    time_range="30m",
    threshold=0.65
)
```

### 3. 智能助手 (Assistant)

**位置**: `app/core/agents/assistant.py`

**功能**:
- 基于RAG的知识问答
- 运维建议和最佳实践
- 上下文理解和记忆
- 多轮对话支持

**技术特性**:
- 向量数据库检索
- 语义搜索
- 上下文管理
- 流式响应

**使用示例**:
```python
from app.core.agents.assistant import AssistantAgent

assistant = AssistantAgent()
response = await assistant.process_query(
    query="如何优化Kubernetes集群性能？",
    context=conversation_history
)
```

### 4. 自动修复 (AutoFix)

**位置**: `app/core/agents/k8s_fixer.py`

**功能**:
- Kubernetes资源自动修复
- 配置优化建议
- 资源扩缩容
- 健康检查修复

**支持的修复类型**:
- Pod重启和恢复
- 资源配额调整
- 网络连接修复
- 存储问题解决

### 5. 监控集成 (Monitoring)

**位置**: `app/services/prometheus.py`

**功能**:
- Prometheus指标查询
- 实时监控数据获取
- 告警规则管理
- 图表数据生成

**支持的指标**:
- 系统资源使用率
- 应用性能指标
- 业务指标
- 自定义指标

## 🔧 API 接口文档

### 健康检查 API

```
GET /api/v1/health
```

**响应示例**:
```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T00:00:00Z",
  "version": "1.0.0",
  "components": {
    "llm": {"status": "healthy", "response_time": 0.1},
    "prometheus": {"status": "healthy", "response_time": 0.05},
    "vector_store": {"status": "healthy", "response_time": 0.02}
  }
}
```

### 负载预测 API

```
GET /api/v1/predict?hours=24&confidence=0.95
```

**响应示例**:
```json
{
  "predictions": [
    {
      "timestamp": "2024-01-01T01:00:00Z",
      "predicted_qps": 150.5,
      "confidence": 0.95,
      "suggested_instances": 3
    }
  ],
  "summary": {
    "max_qps": 200.0,
    "avg_qps": 125.0,
    "recommended_instances": 4
  }
}
```

### 根因分析 API

```
POST /api/v1/rca
Content-Type: application/json

{
  "metrics": ["cpu_usage", "memory_usage", "disk_io"],
  "time_range": "30m",
  "namespace": "default"
}
```

**响应示例**:
```json
{
  "analysis": {
    "root_causes": [
      {
        "cause": "高CPU使用率",
        "confidence": 0.85,
        "affected_resources": ["pod-1", "pod-2"],
        "recommendations": ["扩容Pod副本", "优化CPU配置"]
      }
    ],
    "correlations": [
      {
        "metric_a": "cpu_usage",
        "metric_b": "response_time",
        "correlation": 0.78
      }
    ]
  }
}
```

### 智能助手 API

```
POST /api/v1/assistant/query
Content-Type: application/json

{
  "query": "如何优化Kubernetes集群性能？",
  "context": "生产环境，高负载"
}
```

**响应示例**:
```json
{
  "response": "基于您的生产环境高负载情况，我建议...",
  "sources": [
    {
      "title": "Kubernetes性能优化指南",
      "relevance": 0.92,
      "content": "..."
    }
  ],
  "suggestions": [
    "检查资源配额设置",
    "优化Pod调度策略",
    "配置HPA自动扩缩容"
  ]
}
```

### WebSocket 流式API

```
WS /api/v1/assistant/stream
```

**消息格式**:
```json
{
  "type": "query",
  "data": {
    "query": "用户问题",
    "session_id": "unique-session-id"
  }
}
```

## 🛠️ 开发指南

### 代码规范

1. **命名规范**
   - 使用Python PEP 8标准
   - 类名使用驼峰命名法
   - 函数和变量使用下划线命名法
   - 常量使用大写字母和下划线

2. **文档规范**
   - 所有模块、类、函数都需要docstring
   - 使用中文注释说明复杂逻辑
   - 参数和返回值需要类型注解

3. **错误处理**
   - 使用自定义异常类
   - 记录详细的错误日志
   - 提供有意义的错误消息

### 测试规范

1. **单元测试**
   - 测试文件放在 `tests/` 目录
   - 使用 pytest 作为测试框架
   - 测试覆盖率要求 > 80%

2. **集成测试**
   - API接口测试
   - 数据库连接测试
   - 外部服务集成测试

3. **性能测试**
   - 负载测试
   - 压力测试
   - 内存使用测试

### 部署指南

1. **本地部署**
```bash
# 启动开发环境
python app/main.py
```

2. **Kubernetes部署**
```bash
# TODO: 待实现
```

3. **生产部署**
```bash
# 设置环境变量
export ENV=production

# 启动生产服务
python app/main.py
```

## 📈 性能优化

### 1. 系统性能

- **异步处理**: 使用asyncio处理I/O密集操作
- **连接池**: 数据库和HTTP连接池管理
- **缓存策略**: 多级缓存提升响应速度
- **负载均衡**: 支持水平扩展

### 2. 内存优化

- **对象池**: 复用大对象减少GC压力
- **流式处理**: 大数据集分批处理
- **内存监控**: 实时监控内存使用情况

### 3. 网络优化

- **压缩传输**: 启用gzip压缩
- **长连接**: 复用HTTP连接
- **CDN加速**: 静态资源CDN分发

## 🔒 安全说明

### 1. 数据安全

- **加密存储**: 敏感数据加密存储
- **传输加密**: HTTPS/TLS加密传输
- **访问控制**: 基于角色的访问控制

### 2. API安全

- **身份验证**: JWT令牌认证
- **授权控制**: 细粒度权限控制
- **限流保护**: API请求限流

### 3. 系统安全

- **输入验证**: 严格的输入参数验证
- **SQL注入防护**: 使用参数化查询
- **XSS防护**: 输出数据转义

## 📝 更新日志

### v1.0.0 (2025-07-11)
- 初始版本发布
- 完整的AI-CloudOps功能实现
- 支持多种部署方式
- 完善的API文档和使用指南

### 规划中的功能

- [ ] 图形化Dashboard
- [ ] 更多云平台支持
- [ ] 增强的AI分析能力
- [ ] 移动端应用支持

## 🤝 贡献指南

1. Fork 项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

## 📞 联系我们

- 项目主页: [https://github.com/GoSimplicity/AI-CloudOps]
- 问题报告: [https://github.com/GoSimplicity/AI-CloudOps/issues]
- 邮件联系: [13664854532@163.com]

## 📄 许可证

本项目采用 Apache 2.0 许可证，详情请参阅 [LICENSE](LICENSE) 文件。

---

*本文档最后更新: 2025-07-19*
*版本: 1.0.0*
