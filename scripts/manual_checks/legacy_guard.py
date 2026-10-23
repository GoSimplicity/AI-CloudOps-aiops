"""历史检查仅保留为手动排障资料，必须显式选择隔离环境"""

import os


def require_legacy_opt_in():
    if os.getenv("AIOPS_RUN_LEGACY_CHECKS") != "1":
        raise RuntimeError(
            "历史手工检查可能调用模型、修改集群或发送通知；仅在隔离环境审阅后设置 AIOPS_RUN_LEGACY_CHECKS=1 执行，详见 docs/testing.md"
        )
