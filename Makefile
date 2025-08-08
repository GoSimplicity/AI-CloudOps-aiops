SHELL := /bin/bash
APP_NAME := aiops-platform
IMAGE := aiops/ai-CloudOps:latest

.PHONY: help build push run stop logs test lint fmt clean k8s-apply k8s-delete

help:
	@echo "make build|run|stop|logs|test|lint|fmt|clean"

build:
	docker build -t $(IMAGE) .

push:
	docker push $(IMAGE)

run:
	docker compose up -d --build

stop:
	docker compose down

logs:
	docker compose logs -f --tail=200 $(APP_NAME)

test:
	. .venv/bin/activate && pytest -q

lint:
	. .venv/bin/activate && ruff check .

fmt:
	. .venv/bin/activate && ruff check --fix .

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache logs/*

k8s-apply:
	kubectl apply -f deploy/kubernetes/app.yaml

k8s-delete:
	kubectl delete -f deploy/kubernetes/app.yaml || true

