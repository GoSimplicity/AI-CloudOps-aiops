FROM python:3.11-slim AS builder

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV APP_MODULE=app.main:app
ENV PIP_DEFAULT_TIMEOUT=100
ENV TZ=Asia/Shanghai
ENV ENV=production

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖（关闭版本检查/缓存，加速构建）
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建必要的目录
RUN mkdir -p data/models data/sample logs config

FROM python:3.11-slim
ENV TZ=Asia/Shanghai
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV APP_MODULE=app.main:app

# 安装运行时依赖（无推荐包，清理缓存）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

WORKDIR /app

# 从builder阶段复制已安装的依赖和应用代码
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /app .

# 确保配置目录有正确的权限
RUN chown -R root:root config && chmod -R 755 config

# 创建非root用户
RUN useradd --create-home --shell /bin/bash aiops && \
    chown -R aiops:aiops /app

USER aiops

# 暴露端口
EXPOSE 8080

# 启动应用（生产）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# 健康检查：存活（容器内需有 curl）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fsS http://127.0.0.1:8080/health/live || exit 1