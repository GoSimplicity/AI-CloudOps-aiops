"""普通助手只获得审查过的只读能力，模型输出和远端目录均不是授权来源"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.common.exceptions import AssistantError


class _StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _Calculation(_StrictArguments):
    expression: str = Field(min_length=1, max_length=2000)
    precision: int = Field(default=2, ge=0, le=10)


class _Statistics(_StrictArguments):
    numbers: list[float] = Field(min_length=1, max_length=1000)
    operations: list[Literal["mean", "median", "mode", "std", "variance", "min", "max", "sum", "count"]] = Field(
        default_factory=lambda: ["mean", "median", "std", "min", "max"], max_length=9,
    )


class _Conversion(_StrictArguments):
    value: float
    from_unit: str = Field(min_length=1, max_length=16)
    to_unit: str = Field(min_length=1, max_length=16)
    category: Literal["length", "weight", "temperature", "storage", "time"]


class _Time(_StrictArguments):
    format: Literal["iso", "timestamp"] = "iso"
    # 当前时间工具实际仅产生UTC，不向模型承诺未实现的时区转换
    timezone: Literal["UTC"] = "UTC"


_ResourceName = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")]
_Namespace = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")]


class _Monitor(_StrictArguments):
    operation: Literal["get_node_metrics", "get_pod_metrics", "get_resource_quotas", "get_limit_ranges", "get_top_pods", "get_top_nodes"]
    namespace: _Namespace = "default"
    all_namespaces: bool = False
    node_name: _ResourceName | None = None
    pod_name: _ResourceName | None = None
    max_results: int = Field(default=20, ge=1, le=100)


class _HPA(_StrictArguments):
    operation: Literal["list_hpa", "get_hpa_status"]
    namespace: _Namespace = "default"
    all_namespaces: bool = False
    resource_name: _ResourceName | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.operation == "get_hpa_status" and not self.resource_name:
            raise ValueError("get_hpa_status requires resource_name")
        return self


# 仅这一份静态契约能增加普通助手能力，不读取请求、提示词或目录中的权限开关
_ARGUMENTS = {
    "calculate": (_Calculation, "计算受限算术表达式"),
    "calculate_statistics": (_Statistics, "计算有限数字列表的统计结果"),
    "convert_units": (_Conversion, "进行单位换算"),
    "get_current_time": (_Time, "查询UTC当前时间"),
    "k8s_resource_monitor": (_Monitor, "只读查询当前服务连接集群的资源指标、配额和限制"),
    "k8s_application_scaling": (_HPA, "只读查询HPA列表或指定HPA状态，不允许修改资源"),
}


def readonly_tools(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """目录仅确定工具是否在线，参数、操作及说明使用本地审查过的契约"""
    names = {tool.get("name") for tool in catalog if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
    return [
        {"name": name, "description": description, "parameters": model.model_json_schema()}
        for name, (model, description) in _ARGUMENTS.items() if name in names
    ]


def validate_readonly_call(name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """在每一次实际调度前校验，不能只依靠给模型的JSON Schema"""
    contract = _ARGUMENTS.get(name)
    if contract is None:
        raise AssistantError("普通助手不允许此工具；资源变更请使用独立授权的修复工作流")
    try:
        # 不注入默认字段，保持工具本身既有默认值；返回独立副本避免后续修改原对象
        return contract[0].model_validate(parameters).model_dump(exclude_unset=True)
    except ValidationError as error:
        # 校验异常包含原始参数，不进入日志或HTTP错误正文
        raise AssistantError("普通助手工具参数不符合只读策略，未执行工具") from error
