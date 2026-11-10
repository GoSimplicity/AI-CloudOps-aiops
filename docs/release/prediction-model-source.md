# 预测模型来源

预测用的 `.pkl` 不进 Git，也不随默认镜像分发。进程启动时只从模型目录读取本地文件；文件缺失或损坏时预测仍可用，对应类型走规则兜底。启动不会向 Prometheus 拉数，也不会自动训练。

## 目录

查找目录由 `PREDICTION_MODEL_BASE_PATH` 决定，否则用配置里的 `prediction.model_base_path`。开发配置默认 `data/models`，生产配置默认 `/app/data/models`。`prediction.model_paths` 里的路径只提供文件名，即使写成绝对路径也会落到上面的目录中。

每个类型期望这些文件：

| 类型 | 模型（必需） | 标准化器（可选） | 元数据（可选） |
| --- | --- | --- | --- |
| qps | `qps_prediction_model.pkl` | `qps_prediction_scaler.pkl` | `qps_prediction_model_metadata.json` |
| cpu | `cpu_prediction_model.pkl` | `cpu_prediction_scaler.pkl` | `cpu_prediction_model_metadata.json` |
| memory | `memory_prediction_model.pkl` | `memory_prediction_scaler.pkl` | `memory_prediction_model_metadata.json` |
| disk | `disk_prediction_model.pkl` | `disk_prediction_scaler.pkl` | `disk_prediction_model_metadata.json` |

模型需能被 `joblib` 加载，并对加载时的校验向量提供 `predict`。标准化器缺失时不做额外标准化。元数据缺失时版本和回测准确率保持为空，不会编造。某个类型没有自己的模型、但 QPS 模型可用时，会借用 QPS 模型，预测结果的 `fallback_reason` 为 `使用其他指标的模型`。

## 怎么放进去

任选一种，不要把二进制模型提交进仓库。

`.pkl` 用 joblib 反序列化，加载时会执行文件里的代码。只放置可信来源的模型文件。模型目录和挂载卷不要给其他人写权限。

1. **卷挂载（Compose 默认）**。把文件放到宿主机 `./data/models/`。`docker-compose.yml` 把 `./data` 挂到 `/app/data`，并把 `PREDICTION_MODEL_BASE_PATH` 设为 `/app/data/models`。这个挂载会盖住镜像里的同路径文件，所以 Compose 部署以宿主机目录为准。
2. **其他运行时**。把同一目录挂到容器内的模型路径，或同时修改 `PREDICTION_MODEL_BASE_PATH` 和挂载点。运行用户需要对这些文件有读权限。
3. **打进镜像**。构建上下文里已经有 `data/models` 时执行 `docker build --build-arg INCLUDE_PREDICTION_MODELS=true .`。默认 `false`：上下文里没有模型，或有模型但不想打进镜像，构建都会成功，运行层里的模型目录是空的。若上下文里有大文件但参数为 `false`，这些文件仍会进入构建缓存中的复制层；不想让它们参与构建时，先移走目录或在 `.dockerignore` 中忽略 `data/models`（忽略后就不能再用该参数打入）。

## 训练

本仓库没有可复现的训练入口，`data/run_training.sh` 不在当前树里。训练应在具备真实历史指标的离线环境完成，再用 `joblib` 写出上表中的文件。不要在生产启动时训练。

## 怎么确认在用模型还是规则

启动时如果有类型缺失、加载失败或借用了其他指标的模型，会打 **一条** WARNING。内容包括模型目录、每个有问题的文件路径、当前是规则兜底还是借用模型，以及上面的三种提供方式。模型齐全时改为 INFO，不把“没有标准化器”说成规则兜底。

`GET /api/v1/predict/info` 在不初始化 Redis / Prometheus 的情况下返回新增字段 `prediction_models`：

- `model_base_path`：实际查找目录
- `rule_fallback`：是否至少有一个类型没有可用模型，预测会走规则
- `any_fallback`：是否至少有一个类型处于兜底（含借用其他指标模型）
- `models[]`：`prediction_type`、`loaded`、`fallback`、`status`（`loaded` / `adapted` / `missing` / `load_failed`）、`file_status`（该类型自己的文件是 `loaded`、`missing` 还是 `load_failed`；借用时 `status` 为 `adapted`，`file_status` 仍说明专用文件）、`model_path`（该类型自己的文件路径）、`fallback_reason`

`fallback_reason` 与预测接口一致：没有可用模型时为 `预测模型不可用`，借用其他指标模型时为 `使用其他指标的模型`，专用模型正常加载时为 `null`。预测响应里原有的 `method`、`fallback_mode`、`fallback_reason` 含义不变。`model_base_path` 和 `model_path` 只出现在 `/info`。

`GET /api/v1/predict/ready` 和复用它的 `/predict/health` 成功时也带 `prediction_models`，但只有 `rule_fallback`、`any_fallback`，以及每个模型的 `prediction_type`、`status`、`loaded`、`fallback`。这里没有目录、文件路径、`file_status` 或 `fallback_reason`。配置了服务密钥时，这两个接口和 `/info` 一样要带密钥；密钥为空的开发环境都不校验，所以就绪响应仍然不放路径。模型一个都没加载，或 Prometheus 不可用时，就绪检查仍返回 503；这时看启动日志和 `/info`。

`GET /api/v1/predict/info` 的模型状态反映的是进程启动时读到的情况。之后调用 `reload_model` 或 `save_model` 不会刷新这份结果，要重启进程才会更新。

容器存活探针访问免鉴权的 `GET /health`，不检查模型。没挂模型、走规则兜底时该接口仍返回 200。不要把 `/predict/ready` 当作存活探针。
