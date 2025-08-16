# AI-CloudOps 智能运维平台

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.116+-green.svg)](https://fastapi.tiangolo.com/)

面向云原生的 AIOps 智能运维平台，集成预测分析、根因分析、自动修复、多智能体协作与智能助手等核心能力。基于 FastAPI 构建，支持 Kubernetes 环境，提供完整的运维自动化解决方案。

## 🚀 核心功能

### 📊 智能预测
- **资源预测**: 基于历史数据预测 Pod 副本数需求
- **趋势分析**: 多维度指标趋势预测和异常预警
- **模型管理**: 支持多种机器学习模型，自动模型验证和更新

### 🔍 根因分析 (RCA)
- **多源数据采集**: Prometheus 指标、K8s 事件、容器日志、分布式追踪
- **智能分析**: 统计异常检测、相关性分析、因果推断
- **可视化输出**: 根因候选排名、事件时间线、因果图谱

### 🛠️ 自动修复
- **智能诊断**: 自动识别 K8s 资源问题
- **修复策略**: 基于规则引擎的自动修复方案
- **工作流编排**: 支持复杂的修复流程和回滚机制

### 🤖 多智能体系统
- **协作修复**: 多个专业智能体协同工作
- **策略制定**: 智能体间策略协调和冲突解决
- **执行监控**: 实时监控修复执行状态

### 💬 智能助手
- **RAG 问答**: 基于知识库的智能问答系统
- **对话记忆**: 支持上下文连续对话
- **实时搜索**: 集成网络搜索增强回答质量

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                    AI-CloudOps Platform                     │
├─────────────────────────────────────────────────────────────┤
│  API Layer (FastAPI)                                        │
│  ├── Health Check    ├── Prediction    ├── RCA             │
│  ├── Autofix         ├── Multi-Agent   ├── Assistant       │
├─────────────────────────────────────────────────────────────┤
│  Core Services                                              │
│  ├── LLM Service     ├── K8s Service   ├── Prometheus      │
│  ├── Notification    ├── Tracing       ├── Cache Manager   │
├─────────────────────────────────────────────────────────────┤
│  Data Layer                                                 │
│  ├── Redis Cache     ├── Vector Store  ├── SQLAlchemy      │
│  ├── File Storage    ├── Logs          ├── Metrics         │
├─────────────────────────────────────────────────────────────┤
│  Infrastructure                                             │
│  ├── Docker          ├── Kubernetes    ├── Prometheus      │
│  ├── Grafana         ├── Ollama        ├── Redis           │
└─────────────────────────────────────────────────────────────┘
```

## 🛠️ 技术栈

- **后端框架**: FastAPI + Uvicorn
- **AI/ML**: LangChain, OpenAI, Ollama, scikit-learn
- **数据存储**: Redis, SQLAlchemy, ChromaDB
- **监控**: Prometheus, Grafana
- **容器化**: Docker, Docker Compose
- **编排**: Kubernetes
- **开发工具**: Ruff, Pytest, Make

## 🚀 快速开始

### 环境要求

- Python 3.9+
- Docker & Docker Compose
- Redis
- Prometheus (可选)
- Ollama (可选，用于本地 LLM)

### 本地开发

1. **克隆项目**
```bash
git clone <repository-url>
cd Ai-CloudOps-aiops
```

2. **创建虚拟环境**
```bash
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# 或 .venv\Scripts\activate  # Windows
```

3. **安装依赖**
```bash
pip install -r requirements.txt
```

4. **配置环境变量**
```bash
cp env.example .env
# 编辑 .env 文件，设置必要的 API 密钥和服务地址
```

5. **启动服务**
```bash
# 使用 uvicorn 启动
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080

# 或使用提供的启动脚本
./scripts/start.sh
```

### Docker 部署

```bash
# 使用 Docker Compose 启动完整环境
docker compose up -d --build

# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f aiops-platform
```

## 📚 API 文档

启动服务后，访问以下地址查看 API 文档：
- **Swagger UI**: http://localhost:8080/docs
- **ReDoc**: http://localhost:8080/redoc

### 核心 API 端点

#### 健康检查
- `GET /api/v1/health` - 基础健康检查
- `GET /api/v1/health/detailed` - 详细健康状态
- `GET /api/v1/health/k8s` - Kubernetes 连接状态
- `GET /api/v1/health/prometheus` - Prometheus 连接状态

#### 智能预测
- `POST /api/v1/predict/create` - 创建预测任务
- `POST /api/v1/predict/trend/create` - 趋势预测
- `GET /api/v1/predict/model/info/detail` - 模型信息
- `GET /api/v1/predict/health/detail` - 预测服务健康状态

#### 根因分析
- `POST /api/v1/rca/analyze` - 启动 RCA 分析
- `POST /api/v1/rca/correlate` - 相关性分析
- `GET /api/v1/rca/jobs/detail/{job_id}` - 查询分析任务
- `GET /api/v1/rca/topology/detail` - 获取拓扑信息

#### 自动修复
- `POST /api/v1/autofix/diagnose` - 问题诊断
- `POST /api/v1/autofix/fix` - 执行修复
- `POST /api/v1/autofix/workflow` - 工作流修复
- `GET /api/v1/autofix/records/{id}` - 查询修复记录

#### 智能助手
- `POST /api/v1/assistant/chat/create` - 创建对话
- `POST /api/v1/assistant/search/list` - 知识库搜索
- `GET /api/v1/assistant/health/detail` - 助手服务状态

#### 多智能体
- `POST /api/v1/multi-agent/execute` - 执行多智能体任务
- `GET /api/v1/multi-agent/status/detail` - 查询执行状态
- `GET /api/v1/multi-agent/coordination/detail` - 协调状态

## 🔧 配置说明

### 环境变量优先级
1. 环境变量
2. `config/config.{ENV}.yaml`
3. `config/config.yaml`

### 主要配置项

```yaml
# 应用配置
app:
  host: "0.0.0.0"
  port: 8080
  debug: false
  log_level: "INFO"

# LLM 配置
llm:
  provider: "openai"  # openai, ollama
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL}"

# Kubernetes 配置
kubernetes:
  config_path: "${KUBECONFIG}"
  namespace: "default"

# Prometheus 配置
prometheus:
  host: "${PROMETHEUS_HOST}"
  port: 9090

# Redis 配置
redis:
  host: "${REDIS_HOST}"
  port: 6379
  password: "${REDIS_PASSWORD}"
```

## 🧪 测试

```bash
# 运行所有测试
make test

# 代码检查
make lint

# 代码格式化
make fmt

# 清理缓存
make clean
```

## 📦 部署

### Kubernetes 部署

```bash
# 应用 Kubernetes 配置
make k8s-apply

# 删除部署
make k8s-delete
```

### 生产环境建议

1. **使用生产配置**
   - 设置 `ENV=production`
   - 使用 `config/config.production.yaml`
   - 设置 `debug=false`, `log_level=WARNING`

2. **安全配置**
   - 配置 TLS 证书
   - 设置适当的资源限制
   - 启用 RBAC 权限控制

3. **监控配置**
   - 配置 Prometheus 监控
   - 设置 Grafana 仪表板
   - 配置告警规则

## 📖 详细文档

- [智能助手使用指南](docs/assistant_guide.md)
- [自动修复指南](docs/autofix_guide.md)
- [根因分析文档](docs/rca.md)
- [多智能体架构](docs/k8s_multi_agent_remediation_arch.md)

## 🤝 贡献指南

1. Fork 项目
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开 Pull Request

## 📄 许可证

本项目采用 Apache 2.0 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 📞 联系方式

- 作者: Bamboo
- 邮箱: bamboocloudops@gmail.com
- 项目地址: [GitHub Repository]

---

**注意**: 本项目仍在积极开发中，API 可能会有变化。建议在生产环境使用前充分测试。
