#!/usr/bin/env bash
set -euo pipefail

# 简易一键初始化脚本

ROOT_DIR=$(cd $(dirname $0)/.. && pwd)
cd "$ROOT_DIR"

echo "[INFO] 使用 venv 创建虚拟环境"
python3 -m venv .venv
source .venv/bin/activate

echo "[INFO] 安装依赖"
pip install -U pip
pip install -r requirements.txt

echo "[INFO] 创建配置与目录"
mkdir -p logs data/models data/vector_db
cp -n env.example .env || true

echo "[INFO] 运行测试与静态检查"
pytest -q || true
ruff check . || true

echo "[INFO] 完成。可运行: ./scripts/start.sh"

#!/bin/bash

# AIOps平台环境设置脚本

set -e

echo "🚀 开始设置AIOps平台环境..."

# 检查Python版本
check_python() {
    echo "🐍 检查Python版本..."
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
        echo "Python版本: $PYTHON_VERSION"

        # 检查是否为3.11+
        if python3 -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)"; then
            echo "✅ Python版本满足要求"
        else
            echo "❌ Python版本需要3.11或更高版本"
            exit 1
        fi
    else
        echo "❌ 未找到Python3，请先安装Python 3.11+"
        exit 1
    fi
}

# 检查Docker
check_docker() {
    echo "🐳 检查Docker..."
    if command -v docker &> /dev/null; then
        DOCKER_VERSION=$(docker --version)
        echo "Docker版本: $DOCKER_VERSION"
        echo "✅ Docker已安装"
    else
        echo "❌ 未找到Docker，请先安装Docker"
        exit 1
    fi

    if command -v docker-compose &> /dev/null; then
        COMPOSE_VERSION=$(docker-compose --version)
        echo "Docker Compose版本: $COMPOSE_VERSION"
        echo "✅ Docker Compose已安装"
    else
        echo "❌ 未找到Docker Compose，请先安装Docker Compose"
        exit 1
    fi
}

# 创建必要的目录
create_directories() {
    echo "📁 创建必要的目录..."
    mkdir -p data/models
    mkdir -p data/sample
    mkdir -p logs
    mkdir -p config
    mkdir -p deploy/kubernetes
    mkdir -p deploy/grafana/dashboards
    mkdir -p deploy/grafana/datasources
    mkdir -p deploy/prometheus
    echo "✅ 目录创建完成"
}

# 设置配置文件
setup_config() {
    echo "⚙️  设置配置文件..."

    # 环境变量文件 (仅包含敏感数据)
    if [ ! -f .env ]; then
        cp env.example .env
        echo "✅ 已创建 .env 文件，请根据需要修改API密钥和敏感数据"
    else
        echo "⚠️  .env 文件已存在，跳过创建"
    fi

    # 创建开发环境YAML配置
    if [ ! -f config/config.yaml ]; then
        cat > config/config.yaml << 'EOF'
# 应用基础配置
app:
  debug: true # 是否开启调试模式
  host: 0.0.0.0
  port: 8080
  log_level: INFO

# Prometheus配置
prometheus:
  host: 127.0.0.1:9090
  timeout: 30

# LLM模型配置
llm:
  provider: openai # 可选值: openai, ollama
  model: Qwen/Qwen3-14B # 主模型
  task_model: Qwen/Qwen2.5-14B-Instruct # 任务模型
  temperature: 0.7 # LLM模型温度
  max_tokens: 2048 # LLM模型最大生成长度
  request_timeout: 15 # LLM请求超时时间(秒)
  # Ollama模型配置
  ollama_model: qwen2.5:3b
  ollama_base_url: http://127.0.0.1:11434

# 测试配置
testing:
  skip_llm_tests: false # 设置为true可跳过依赖LLM的测试

# Kubernetes配置
kubernetes:
  in_cluster: false # 是否使用Kubernetes集群内配置
  config_path: ./deploy/kubernetes/config # Kubernetes集群配置文件路径
  namespace: default

# 根因分析配置
rca:
  default_time_range: 30 # 默认时间范围(分钟)
  max_time_range: 1440 # 最大时间范围(分钟)
  anomaly_threshold: 0.65  # 根因分析异常阈值
  correlation_threshold: 0.7 # 根因分析相关度阈值
  default_metrics: # 默认监控指标
    - container_cpu_usage_seconds_total
    - container_memory_working_set_bytes
    - kube_pod_container_status_restarts_total
    - kube_pod_status_phase
    - node_cpu_seconds_total
    - node_memory_MemFree_bytes
    - kubelet_http_requests_duration_seconds_count
    - kubelet_http_requests_duration_seconds_sum

# 预测配置
prediction:
  model_path: data/models/time_qps_auto_scaling_model.pkl # 预测模型路径
  scaler_path: data/models/time_qps_auto_scaling_scaler.pkl # 预测模型缩放器路径
  max_instances: 20 # 预测模型最大实例数
  min_instances: 1 # 预测模型最小实例数
  prometheus_query: 'rate(nginx_ingress_controller_nginx_process_requests_total{service="ingress-nginx-controller-metrics"}[10m])' # 预测模型查询

# 通知配置
notification:
  enabled: true # 是否启用通知

# Redis配置 - 用于向量数据缓存和元数据存储
redis:
  host: 127.0.0.1
  port: 6379
  db: 0
  password: "v6SxhWHyZC7S"
  connection_timeout: 5 # Redis连接超时时间(秒)
  socket_timeout: 5 # RedisSocket超时时间(秒)
  max_connections: 10 # Redis最大连接数
  decode_responses: true # 是否解码响应

# 小助手配置
rag:
  vector_db_path: data/vector_db # 向量数据库路径
  collection_name: aiops-assistant # 向量数据库集合名称
  knowledge_base_path: data/knowledge_base # 知识库路径
  chunk_size: 1000 # 文档分块大小
  chunk_overlap: 200 # 文档分块重叠大小
  top_k: 4 # 最多返回的相似度
  similarity_threshold: 0.7 # 相似度阈值
  openai_embedding_model: Pro/BAAI/bge-m3 # OpenAI嵌入模型
  ollama_embedding_model: nomic-embed-text # Ollama嵌入模型
  max_context_length: 4000 # 最大上下文长度
  temperature: 0.1 # LLM模型温度
  cache_expiry: 3600 # 缓存过期时间(秒)
  max_docs_per_query: 8 # 每次查询最多处理的文档数
  use_enhanced_retrieval: true # 是否使用增强检索
  use_document_compressor: true # 是否使用文档压缩

# MCP配置
mcp:
  server_url: "http://127.0.0.1:9000" # MCP服务端地址
  timeout: 30 # 请求超时时间(秒)
  max_retries: 3 # 最大重试次数
  health_check_interval: 5 # 健康检查间隔(秒)
EOF
        echo "✅ 已创建开发环境配置文件 config/config.yaml"
    else
        echo "⚠️  config/config.yaml 文件已存在，跳过创建"
    fi

    # 创建生产环境YAML配置
    if [ ! -f config/config.production.yaml ]; then
        cat > config/config.production.yaml << 'EOF'
# 应用基础配置
app:
  debug: false # 是否开启调试模式
  host: 0.0.0.0
  port: 8080
  log_level: WARNING

# Prometheus配置
prometheus:
  host: 127.0.0.1:9090
  timeout: 30

# LLM模型配置
llm:
  provider: openai # 可选值: openai, ollama
  model: Qwen/Qwen3-14B # 主模型
  task_model: Qwen/Qwen2.5-14B-Instruct # 任务模型
  temperature: 0.7 # LLM模型温度
  max_tokens: 2048 # LLM模型最大生成长度
  request_timeout: 15 # LLM请求超时时间(秒)
  # Ollama模型配置
  ollama_model: qwen2.5:3b
  ollama_base_url: http://127.0.0.1:11434

# 测试配置
testing:
  skip_llm_tests: true # 设置为true可跳过依赖LLM的测试

# Kubernetes配置
kubernetes:
  in_cluster: false # 是否使用Kubernetes集群内配置
  config_path: ./deploy/kubernetes/config # Kubernetes集群配置文件路径
  namespace: default

# 根因分析配置
rca:
  default_time_range: 30 # 默认时间范围(分钟)
  max_time_range: 1440 # 最大时间范围(分钟)
  anomaly_threshold: 0.65  # 根因分析异常阈值
  correlation_threshold: 0.7 # 根因分析相关度阈值
  default_metrics: # 默认监控指标
    - container_cpu_usage_seconds_total
    - container_memory_working_set_bytes
    - kube_pod_container_status_restarts_total
    - kube_pod_status_phase
    - node_cpu_seconds_total
    - node_memory_MemFree_bytes
    - kubelet_http_requests_duration_seconds_count
    - kubelet_http_requests_duration_seconds_sum

# 预测配置
prediction:
  model_path: data/models/time_qps_auto_scaling_model.pkl # 预测模型路径
  scaler_path: data/models/time_qps_auto_scaling_scaler.pkl # 预测模型缩放器路径
  max_instances: 20 # 预测模型最大实例数
  min_instances: 1 # 预测模型最小实例数
  prometheus_query: 'rate(nginx_ingress_controller_nginx_process_requests_total{service="ingress-nginx-controller-metrics"}[10m])' # 预测模型查询

# 通知配置
notification:
  enabled: true # 是否启用通知

# Redis配置 - 用于向量数据缓存和元数据存储
redis:
  host: 127.0.0.1
  port: 6379
  db: 0
  password: "v6SxhWHyZC7S"
  connection_timeout: 5 # Redis连接超时时间(秒)
  socket_timeout: 5 # RedisSocket超时时间(秒)
  max_connections: 10 # Redis最大连接数
  decode_responses: true # 是否解码响应

# 小助手配置
rag:
  vector_db_path: data/vector_db # 向量数据库路径
  collection_name: aiops-assistant # 向量数据库集合名称
  knowledge_base_path: data/knowledge_base # 知识库路径
  chunk_size: 1000 # 文档分块大小
  chunk_overlap: 200 # 文档分块重叠大小
  top_k: 4 # 最多返回的相似度
  similarity_threshold: 0.7 # 相似度阈值
  openai_embedding_model: Pro/BAAI/bge-m3 # OpenAI嵌入模型
  ollama_embedding_model: nomic-embed-text # Ollama嵌入模型
  max_context_length: 4000 # 最大上下文长度
  temperature: 0.1 # LLM模型温度
  cache_expiry: 3600 # 缓存过期时间(秒)
  max_docs_per_query: 8 # 每次查询最多处理的文档数
  use_enhanced_retrieval: true # 是否使用增强检索
  use_document_compressor: true # 是否使用文档压缩

# MCP配置
mcp:
  server_url: "http://127.0.0.1:9000" # MCP服务端地址
  timeout: 30 # 请求超时时间(秒)
  max_retries: 3 # 最大重试次数
  health_check_interval: 5 # 健康检查间隔(秒)
EOF
        echo "✅ 已创建生产环境配置文件 config/config.production.yaml"
    else
        echo "⚠️  config/config.production.yaml 文件已存在，跳过创建"
    fi
}

# 安装Python依赖
install_python_deps() {
    echo "📦 安装Python依赖..."

    # 检查是否在虚拟环境中
    if [[ "$VIRTUAL_ENV" != "" ]]; then
        echo "✅ 检测到虚拟环境: $VIRTUAL_ENV"
    else
        echo "⚠️  建议在虚拟环境中安装依赖"
        read -p "是否继续在系统环境中安装？(y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "请先创建并激活虚拟环境："
            echo "  python3 -m venv venv"
            echo "  source venv/bin/activate"
            exit 1
        fi
    fi

    pip install --upgrade pip
    pip install -r requirements.txt
    echo "✅ Python依赖安装完成"
}

# 创建示例配置文件
create_sample_configs() {
    echo "📝 创建示例配置文件..."

    # Prometheus配置
    cat > deploy/prometheus/prometheus.yml << 'EOF'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "rules/*.yml"

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'aiops-platform'
    static_configs:
      - targets: ['aiops-platform:8080']
    metrics_path: '/api/v1/health/metrics'
    scrape_interval: 30s

  - job_name: 'kubernetes-pods'
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: true
EOF

    # Grafana数据源配置
    mkdir -p deploy/grafana/datasources
    cat > deploy/grafana/datasources/prometheus.yml << 'EOF'
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
EOF

    # 创建Kubernetes配置示例文件
    mkdir -p deploy/kubernetes
    cat > deploy/kubernetes/config.example << 'EOF'
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://kubernetes.default.svc
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
  name: default
contexts:
- context:
    cluster: default
    namespace: default
    user: default
  name: default
current-context: default
users:
- name: default
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
EOF

    echo "✅ 示例配置文件创建完成"
}

# 下载示例模型文件（如果需要）
download_sample_models() {
    echo "🤖 检查模型文件..."

    if [ ! -f "data/models/time_qps_auto_scaling_model.pkl" ]; then
        echo "⚠️  未找到预测模型文件"
        echo "请将训练好的模型文件放置在 data/models/ 目录下："
        echo "  - time_qps_auto_scaling_model.pkl"
        echo "  - time_qps_auto_scaling_scaler.pkl"
        echo "或者运行训练脚本生成模型"
    else
        echo "✅ 模型文件已存在"
    fi
}

# 验证安装
verify_installation() {
    echo "🔍 验证安装..."

    # 检查Python导入
    python3 -c "
import fastapi
import pandas
import numpy
import sklearn
import yaml
import requests
print('✅ 主要Python包导入成功')
"

    # 检查应用能否启动（语法检查）
    python3 -c "
import sys
sys.path.append('.')
try:
    from app.main import app
    import fastapi
    print('✅ 应用代码语法检查通过')
except Exception as e:
    print(f'❌ 应用代码检查失败: {str(e)}')
    sys.exit(1)
"

    echo "✅ 安装验证完成"
}

# 配置Kubernetes
setup_kubernetes() {
    echo "☸️  配置Kubernetes环境..."

    # 检查是否存在kubeconfig
    if [ -f "$HOME/.kube/config" ]; then
        echo "✅ 检测到Kubernetes配置文件"
        # 复制到项目目录
        mkdir -p deploy/kubernetes
        cp "$HOME/.kube/config" deploy/kubernetes/config
        echo "✅ 已复制Kubernetes配置到项目目录"
    else
        echo "⚠️  未找到Kubernetes配置文件"
        echo "请确保您有权限访问Kubernetes集群，并将配置文件放置在以下位置之一："
        echo "  - $HOME/.kube/config"
        echo "  - deploy/kubernetes/config"

        # 创建示例配置
        echo "已创建示例配置文件，请根据实际情况修改："
        echo "  - deploy/kubernetes/config.example"
    fi
}

# 显示下一步操作
show_next_steps() {
    echo ""
    echo "🎉 AIOps平台环境设置完成！"
    echo ""
    echo "下一步操作："
    echo "1. 配置文件："
    echo "   - 编辑 config/config.yaml 文件配置应用参数"
    echo "   - 编辑 .env 文件配置API密钥和敏感数据"
    echo "2. 确保Kubernetes配置正确（如果使用K8s功能）"
    echo "   - 检查 deploy/kubernetes/config 文件"
    echo "3. 启动服务："
    echo "   # 使用Docker Compose（推荐）"
    echo "   docker-compose up -d"
    echo ""
    echo "   # 或本地开发模式"
    echo "   ENV=development ./scripts/start.sh start"
    echo ""
    echo "   # 或生产环境"
    echo "   ENV=production ./scripts/start.sh start"
    echo ""
    echo "4. 访问服务："
    echo "   - AIOps API: http://localhost:8080"
    echo "   - Prometheus: http://localhost:9090"
    echo "   - Grafana: http://localhost:3000 (admin/admin123)"
    echo ""
    echo "5. 健康检查："
    echo "   curl http://localhost:8080/api/v1/health"
    echo ""
}

# 主函数
main() {
    echo "AIOps平台环境设置脚本"
    echo "========================"

    check_python
    check_docker
    create_directories
    setup_config
    install_python_deps
    create_sample_configs
    setup_kubernetes
    download_sample_models
    verify_installation
    show_next_steps
}

# 处理中断信号
trap 'echo "❌ 设置被中断"; exit 1' INT

# 运行主函数
main "$@"
