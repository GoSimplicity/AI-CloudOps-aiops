# AI-CloudOps 部署配置指南

## 概述

本指南提供AI-CloudOps平台的完整部署和配置说明，包括环境准备、安装步骤、配置优化和常见问题解决方案。

## 环境要求

### 基础环境
- **操作系统**：Ubuntu 20.04+ / CentOS 8+ / macOS 12+
- **Python版本**：Python 3.9+
- **内存要求**：最低8GB，推荐16GB+
- **存储空间**：最低50GB可用空间
- **网络要求**：稳定的互联网连接（用于AI模型访问）

### 依赖服务
- **Redis**：版本6.0+（用于向量存储和缓存）
- **Docker**：版本20.10+（容器化部署）
- **Kubernetes**：版本1.24+（集群部署）
- **Prometheus**：版本2.30+（监控数据源）

## 快速部署

### 方式一：Docker Compose 部署（推荐新手）

#### 1. 克隆项目代码
```bash
git clone https://github.com/your-org/ai-cloudops-aiops.git
cd ai-cloudops-aiops
```

#### 2. 配置环境变量
```bash
# 复制环境变量模板
cp env.example .env

# 编辑环境变量
vim .env
```

#### 3. 启动服务
```bash
# 启动所有服务
docker-compose up -d

# 查看服务状态
docker-compose ps

# 查看日志
docker-compose logs -f
```

### 方式二：本地开发部署

#### 1. 创建Python虚拟环境
```bash
python3 -m venv aiops-env
source aiops-env/bin/activate  # Linux/macOS
# 或者 aiops-env\Scripts\activate  # Windows
```

#### 2. 安装Python依赖
```bash
pip install -r requirements.txt
```

#### 3. 启动Redis服务
```bash
# 使用Docker启动Redis
docker run -d \
  --name aiops-redis \
  -p 6379:6379 \
  redis:7-alpine

# 或者使用系统包管理器安装
sudo apt-get install redis-server  # Ubuntu
brew install redis  # macOS
```

#### 4. 配置应用
```bash
# 复制配置模板
cp config/config.yaml.example config/config.yaml

# 编辑配置文件
vim config/config.yaml
```

#### 5. 启动应用
```bash
python app/main.py
```

## 详细配置说明

### 核心配置文件

#### config/config.yaml
```yaml
# 应用基础配置
app:
  host: "0.0.0.0"
  port: 8000
  debug: false
  workers: 4

# LLM配置
llm:
  provider: "openai"  # 选择: openai, ollama
  model: "gpt-3.5-turbo"
  api_key: "your-openai-api-key"
  base_url: "https://api.openai.com/v1"
  temperature: 0.7
  
  # Ollama配置（如果使用本地模型）
  ollama_base_url: "http://localhost:11434"
  ollama_model: "llama2"

# Redis配置
redis:
  host: "localhost"
  port: 6379
  db: 0
  password: ""
  connection_timeout: 5
  socket_timeout: 5
  max_connections: 10

# RAG配置
rag:
  vector_db_path: "data/vector_db"
  knowledge_base_path: "data/knowledge_base"
  collection_name: "aiops_docs"
  chunk_size: 1000
  chunk_overlap: 200
  
  # 嵌入模型配置
  openai_embedding_model: "text-embedding-ada-002"
  ollama_embedding_model: "llama2:embedding"

# 日志配置
logging:
  level: "INFO"
  file: "logs/aiops.log"
  max_bytes: 10485760  # 10MB
  backup_count: 5
```

### 环境变量配置

#### .env 文件
```bash
# 应用配置
APP_HOST=0.0.0.0
APP_PORT=8000
APP_DEBUG=false

# OpenAI配置
OPENAI_API_KEY=your-openai-api-key-here
OPENAI_BASE_URL=https://api.openai.com/v1

# Redis配置
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# 其他配置
LOG_LEVEL=INFO
PYTHONPATH=/app
```

## Kubernetes部署

### 1. 准备Kubernetes清单文件

#### namespace.yaml
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: aiops-system
```

#### redis-deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  namespace: aiops-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
      - name: redis
        image: redis:7-alpine
        ports:
        - containerPort: 6379
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
---
apiVersion: v1
kind: Service
metadata:
  name: redis-service
  namespace: aiops-system
spec:
  selector:
    app: redis
  ports:
  - port: 6379
    targetPort: 6379
```

#### aiops-deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aiops-api
  namespace: aiops-system
spec:
  replicas: 2
  selector:
    matchLabels:
      app: aiops-api
  template:
    metadata:
      labels:
        app: aiops-api
    spec:
      containers:
      - name: aiops-api
        image: aiops/api:latest
        ports:
        - containerPort: 8000
        env:
        - name: REDIS_HOST
          value: "redis-service"
        - name: OPENAI_API_KEY
          valueFrom:
            secretKeyRef:
              name: aiops-secrets
              key: openai-api-key
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: aiops-api-service
  namespace: aiops-system
spec:
  selector:
    app: aiops-api
  ports:
  - port: 80
    targetPort: 8000
  type: LoadBalancer
```

### 2. 部署到Kubernetes
```bash
# 创建namespace
kubectl apply -f namespace.yaml

# 创建Secret（替换为你的实际API密钥）
kubectl create secret generic aiops-secrets \
  --from-literal=openai-api-key=your-openai-api-key \
  -n aiops-system

# 部署Redis
kubectl apply -f redis-deployment.yaml

# 部署AI-CloudOps API
kubectl apply -f aiops-deployment.yaml

# 查看部署状态
kubectl get pods -n aiops-system
kubectl get services -n aiops-system
```

## 性能优化配置

### 1. Redis优化
```bash
# redis.conf 优化配置
maxmemory 2gb
maxmemory-policy allkeys-lru
save 900 1
save 300 10
save 60 10000

# 内存优化
hash-max-ziplist-entries 512
hash-max-ziplist-value 64
list-max-ziplist-size -2
set-max-intset-entries 512
```

### 2. 应用性能优化
```yaml
# config/config.yaml 性能配置
app:
  workers: 8  # 根据CPU核心数调整
  worker_connections: 1000
  keepalive_timeout: 65

# 缓存配置
cache:
  enabled: true
  ttl: 3600  # 1小时
  max_size: 1000

# 向量检索优化
rag:
  batch_size: 50
  max_docs_per_query: 10
  similarity_threshold: 0.7
```

### 3. 数据库连接池优化
```yaml
redis:
  max_connections: 20
  connection_timeout: 10
  socket_timeout: 10
  retry_on_timeout: true
  health_check_interval: 30
```

## 监控配置

### 1. Prometheus监控
```yaml
# prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'aiops-api'
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
    scrape_interval: 10s
```



## 安全配置

### 1. API安全
```yaml
security:
  cors:
    enabled: true
    origins: ["https://your-domain.com"]
  
  rate_limiting:
    enabled: true
    requests_per_minute: 100
  
  api_keys:
    enabled: false  # 生产环境建议启用
```

### 2. 网络安全
```bash
# 防火墙配置
sudo ufw allow 8000/tcp  # API端口
sudo ufw allow 6379/tcp  # Redis端口（仅内网）
sudo ufw enable
```

## 常见问题和解决方案

### 1. Redis连接问题
```bash
# 问题：Redis连接超时
# 解决方案：
# 1. 检查Redis服务状态
sudo systemctl status redis

# 2. 检查端口是否被占用
netstat -tlnp | grep 6379

# 3. 检查Redis配置
redis-cli ping
```

### 2. AI模型访问问题
```bash
# 问题：OpenAI API调用失败
# 解决方案：
# 1. 验证API密钥
curl -H "Authorization: Bearer ${AIOPS_API_KEY}" \
  https://api.openai.com/v1/models

# 2. 检查网络连接
ping api.openai.com

# 3. 查看应用日志
tail -f logs/aiops.log
```

### 3. 内存不足问题
```bash
# 问题：应用内存占用过高
# 解决方案：
# 1. 调整缓存大小
# 2. 优化批处理大小
# 3. 增加系统内存
# 4. 启用内存监控
```

### 4. 知识库更新问题
```bash
# 问题：知识库文档更新后不生效
# 解决方案：
# 1. 刷新知识库
curl -X POST http://localhost:8000/api/v1/assistant/refresh

# 2. 清除缓存
curl -X POST http://localhost:8000/api/v1/assistant/clear-cache

# 3. 重启应用
docker-compose restart
```

## 备份和恢复

### 1. 数据备份
```bash
# Redis数据备份
redis-cli BGSAVE
cp /var/lib/redis/dump.rdb /backup/redis-$(date +%Y%m%d).rdb

# 配置文件备份
tar -czf /backup/aiops-config-$(date +%Y%m%d).tar.gz config/
```

### 2. 数据恢复
```bash
# Redis数据恢复
sudo systemctl stop redis
cp /backup/redis-20240101.rdb /var/lib/redis/dump.rdb
sudo systemctl start redis

# 配置文件恢复
tar -xzf /backup/aiops-config-20240101.tar.gz
```

## 升级指南

### 1. 版本升级步骤
```bash
# 1. 备份当前版本
docker-compose down
cp -r . ../aiops-backup-$(date +%Y%m%d)

# 2. 拉取新版本
git pull origin main

# 3. 更新依赖
pip install -r requirements.txt --upgrade

# 4. 运行数据库迁移（如需要）
python scripts/migrate.py

# 5. 重启服务
docker-compose up -d
```

### 2. 配置兼容性检查
```bash
# 检查配置文件格式
python scripts/check_config.py

# 验证服务健康状态
curl http://localhost:8000/health
```

---

*如有部署问题，请参考故障排查手册或联系技术支持团队。*