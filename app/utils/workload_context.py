"""模型上下文使用独立副本，内部修复仍保留真实工作负载对象"""

from copy import deepcopy


def model_workload_context(workload: dict) -> dict:
    result = deepcopy(workload)
    spec = result.get("spec") or {}
    template = spec.get("template") or {}
    for metadata in [result.get("metadata") or {}, template.get("metadata") or {}]:
        annotations = metadata.get("annotations") or {}
        annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    for pod_spec in [spec, template.get("spec") or {}]:
        for field in ["containers", "init_containers", "ephemeral_containers", "initContainers", "ephemeralContainers"]:
            for container in pod_spec.get(field) or []:
                for entry in container.get("env") or []:
                    if "value" in entry:
                        entry["value"] = "[REDACTED]"
    return result
