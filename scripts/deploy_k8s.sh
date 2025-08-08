#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd $(dirname $0)/.. && pwd)
cd "$ROOT_DIR"

echo "[INFO] 构建并推送镜像(如需)"
docker build -t aiops/ai-CloudOps:latest .

echo "[INFO] 应用K8s清单"
kubectl apply -f deploy/kubernetes/app.yaml

echo "[INFO] 等待Pod运行"
kubectl -n aiops rollout status deploy/aiops-platform --timeout=120s || true

echo "[INFO] Service 地址"
kubectl -n aiops get svc aiops-platform -o wide

