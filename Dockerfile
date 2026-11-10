FROM python:3.11.18-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

# 设置维护者信息
LABEL maintainer="AIOps Team"
LABEL description="AIOps Platform - Root Cause Analysis and Auto-fixing System"

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=100
ENV ENV=production

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --only-binary=:all: -r requirements.txt

# 创建必要的目录
RUN mkdir -p data/models data/sample logs config

# 复制应用代码
COPY . .

# 默认不把模型打进最终镜像层，因此构建上下文里没有 data/models 也能构建。
# 只有 INCLUDE_PREDICTION_MODELS=true 时才保留 COPY 进来的模型文件。
ARG INCLUDE_PREDICTION_MODELS=false
RUN if [ "$INCLUDE_PREDICTION_MODELS" != "true" ]; then \
      rm -rf data/models && mkdir -p data/models; \
    fi \
 && useradd --create-home --uid 10001 aiops && chown -R aiops:aiops /app
USER aiops

# 暴露端口
EXPOSE 8080

# 存活探针只访问免鉴权的 GET /health。使用标准库，不为探针安装 curl。
# 间隔、超时、重试和启动宽限与发布栈 aiops 服务一致；进程内超时短于 --timeout。
# 运行用户已是非 root（aiops，uid 10001）。
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=10 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=3)"]

# 启动应用
CMD ["python", "-m", "app.main"]
