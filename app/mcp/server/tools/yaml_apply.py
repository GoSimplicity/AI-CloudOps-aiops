"""高权限命名空间资源SSA：全部验证和服务端dry-run通过后才逐项写入"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from functools import partial
import json
import os
import re
from typing import Any

from kubernetes.dynamic import DynamicClient
import yaml

MAX_YAML_BYTES = 1024 * 1024
MAX_DOCUMENTS = 20
FIELD_MANAGER = "ai-cloudops-mcp"
REQUEST_TIMEOUT = (5, 15)
_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_NAMESPACE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


# 失败时只按 HTTP status 给固定原因文本；远端 reason/body、str(error) 一律不回传
API_STATUS_REASONS = {400: "请求格式错误", 403: "Kubernetes RBAC或准入拒绝", 404: "资源或命名空间不存在", 409: "字段所有权冲突，未强制覆盖", 422: "资源结构无效"}
UNCERTAIN_API_REASON = "Kubernetes请求未得到确定成功响应"


def safe_error_fields(error: Exception) -> dict:
    """失败返回的安全字段：异常类型名 + ApiException 的 status 与固定原因文本（与 apply_yaml 的 errors 项一致）"""
    status = getattr(error, "status", None)
    status = status if isinstance(status, int) else None
    return {"message": type(error).__name__, "error_type": type(error).__name__, "api_status": status, "reason": API_STATUS_REASONS.get(status, UNCERTAIN_API_REASON)}


class BoundedDynamicClient(DynamicClient):
    """发现API与实际请求均有网络期限，PATCH不做隐式重试"""

    def request(self, *args, **kwargs):
        kwargs.setdefault("_request_timeout", REQUEST_TIMEOUT)
        return super().request(*args, **kwargs)


class ApplyValidationError(ValueError):
    pass


class _ManifestLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ApplyValidationError("YAML对象键必须为不重复的字符串")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _parse(parameters):
    if set(parameters) - {"operation", "config_path", "namespace", "yaml_content"}:
        raise ApplyValidationError("apply_yaml包含不支持的参数，不接受强制覆盖字段所有权")
    config_path = parameters.get("config_path")
    # 与门禁 has_custom_config_path 一致：缺省、None、空串都按默认 kubeconfig 处理
    if config_path not in (None, "") and (not isinstance(config_path, str) or not os.path.isfile(config_path)):
        raise ApplyValidationError("显式kubeconfig必须是存在的文件，拒绝回退到其他集群")
    content = parameters.get("yaml_content")
    if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > MAX_YAML_BYTES:
        raise ApplyValidationError("YAML必须为非空UTF-8文本且不超过1MiB")
    depth = documents = nodes = 0
    for event in yaml.parse(content):
        nodes += 1
        if nodes > 20000 or isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise ApplyValidationError("YAML不支持锚点/别名，或节点数超限")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > 64:
                raise ApplyValidationError("YAML嵌套超过64层")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
        elif isinstance(event, yaml.DocumentStartEvent):
            documents += 1
            if documents > MAX_DOCUMENTS:
                raise ApplyValidationError("YAML最多20个文档")
    resources = [item for item in yaml.load_all(content, Loader=_ManifestLoader) if item is not None]
    if not resources:
        raise ApplyValidationError("YAML没有资源文档")
    namespace = parameters.get("namespace")
    if "namespace" in parameters and (not isinstance(namespace, str) or len(namespace) > 63 or not _NAMESPACE.fullmatch(namespace)):
        raise ApplyValidationError("目标命名空间无效")
    identities = []
    seen = set()
    for item in resources:
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise ApplyValidationError("每个文档必须是含metadata的资源对象")
        meta = item["metadata"]
        version, kind, name = item.get("apiVersion"), item.get("kind"), meta.get("name")
        if not isinstance(version, str) or not re.fullmatch(r"(?:[a-z0-9][a-z0-9.-]*/)?v[0-9][a-z0-9]*", version):
            raise ApplyValidationError("每个资源必须有合法apiVersion")
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", kind) or kind == "List":
            raise ApplyValidationError("每个文档必须有明确kind，不支持List或子资源")
        if not isinstance(name, str) or len(name) > 253 or not _NAME.fullmatch(name):
            raise ApplyValidationError("资源必须有合法metadata.name，不支持generateName")
        if set(meta) & {"generateName", "managedFields", "resourceVersion", "uid", "creationTimestamp", "deletionTimestamp"} or "status" in item:
            raise ApplyValidationError("不得提交generateName、status或服务端管理的metadata")
        target = meta.get("namespace", namespace or "default")
        if not isinstance(target, str) or len(target) > 63 or not _NAMESPACE.fullmatch(target) or (namespace is not None and target != namespace):
            raise ApplyValidationError("资源namespace无效或与显式目标命名空间不一致")
        meta["namespace"] = target
        key = (version, kind, target, name)
        if key in seen:
            raise ApplyValidationError("同一资源不能在一次请求中重复定义")
        seen.add(key)
        identities.append(dict(apiVersion=version, kind=kind, namespace=target, name=name))
    json.dumps(resources, allow_nan=False)
    return resources, identities


async def apply_yaml(tool, clients: dict[str, Any] | None, parameters: dict[str, Any]):
    result = {"success": False, "operation": "apply_yaml", "phase": "validation", "applied_resources": [], "errors": [],
              "not_attempted": [], "outcome_unknown": False, "retry_safe": False, "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        resources, identities = _parse(parameters)
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError, RecursionError) as error:
        result["message"] = "YAML校验失败：需1MiB内、最多20个不重复的有效命名空间资源；不支持别名、非有限JSON及服务端字段"
        if isinstance(error, ApplyValidationError):
            result["message"] = str(error)
        return result

    result["not_attempted"] = deepcopy(identities)
    loop = asyncio.get_running_loop()

    async def run(function, **kwargs):
        return await loop.run_in_executor(tool._executor, partial(function, **kwargs))

    index = 0
    try:
        result["phase"] = "discovery"
        if clients is None:
            clients = await run(tool._initialize_clients, config_path=parameters.get("config_path") or None)
        dynamic = clients.get("dynamic")
        if dynamic is None:
            raise ApplyValidationError("动态API客户端不可用")
        targets = []
        discovered_names = set()
        for index, identity in enumerate(identities):
            target = await run(dynamic.resources.get, api_version=identity["apiVersion"], kind=identity["kind"])
            if target.namespaced is not True or "patch" not in target.verbs:
                raise ApplyValidationError("只支持预先存在命名空间中的可PATCH资源，不支持集群资源")
            canonical = (target.group, target.name, identity["namespace"], identity["name"])
            if canonical in discovered_names:
                raise ApplyValidationError("不同apiVersion/kind不能重复指定同一资源")
            discovered_names.add(canonical)
            targets.append(target)

        # 服务端schema、RBAC、准入及字段冲突全部预检，不持久化任何资源
        result["phase"] = "dry_run"
        for index, target in enumerate(targets):
            await run(target.server_side_apply, body=deepcopy(resources[index]), namespace=identities[index]["namespace"],
                      field_manager=FIELD_MANAGER, force_conflicts=False, dry_run="All", _request_timeout=REQUEST_TIMEOUT,
                      query_params=[("fieldValidation", "Strict")])

        result["phase"] = "apply"
        for index, target in enumerate(targets):
            response = await run(target.server_side_apply, body=deepcopy(resources[index]), namespace=identities[index]["namespace"],
                                 field_manager=FIELD_MANAGER, force_conflicts=False, _request_timeout=REQUEST_TIMEOUT,
                                 query_params=[("fieldValidation", "Strict")])
            data = response.to_dict() if callable(getattr(response, "to_dict", None)) else response
            meta = data.get("metadata", {}) if isinstance(data, dict) else {}
            identity = identities[index]
            if meta.get("name") != identity["name"] or meta.get("namespace") != identity["namespace"] or not meta.get("uid") or not meta.get("resourceVersion"):
                raise ApplyValidationError("API未返回匹配资源的持久化确认")
            result["applied_resources"].append({**identity, "uid": meta["uid"], "resource_version": meta["resourceVersion"]})
            result["not_attempted"] = deepcopy(identities[index + 1:])
        result.update(success=True, phase="completed", message=f"Kubernetes已确认应用{len(resources)}个资源")
    except Exception as error:
        status = getattr(error, "status", None)
        status = status if isinstance(status, int) else None
        # 远端异常body或str(error)可能含Secret文档，不回传、不写日志
        identity = identities[index]
        reason = API_STATUS_REASONS.get(status, UNCERTAIN_API_REASON)
        if isinstance(error, ApplyValidationError):
            reason = str(error)
        elif type(error).__name__ in {"ResourceNotFoundError", "ResourceNotUniqueError"}:
            reason = "apiVersion/kind未知或不能唯一发现"
        result["errors"] = [{**identity, "api_status": status, "error_type": type(error).__name__, "reason": reason}]
        result["outcome_unknown"] = result["phase"] == "apply" and (status is None or status >= 500)
        if result["phase"] == "apply":
            result["not_attempted"] = deepcopy(identities[index + 1:])
        result["message"] = "应用未全部完成，请核对已应用及失败资源的实际状态，勿盲目重试" if result["phase"] == "apply" else "资源发现或服务端预检失败，未持久化任何资源"
    return result
