"""无I/O的知识刷新预算，供配置加载和文件快照共用。"""

from dataclasses import dataclass

DEFAULT_REFRESH_FILES = 128
MAX_REFRESH_FILES = 1024
DEFAULT_REFRESH_TOTAL_BYTES = 32 * 1024 * 1024
MAX_REFRESH_TOTAL_BYTES = 256 * 1024 * 1024
MAX_KNOWLEDGE_FILE_BYTES = 10 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 4096


def parse_refresh_budget_int(value):
    """环境字符串或YAML整数；拒绝bool/浮点和静默截断。"""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise ValueError("知识刷新预算必须为整数")


@dataclass(frozen=True)
class KnowledgeRefreshBudget:
    max_files: int = DEFAULT_REFRESH_FILES
    max_total_bytes: int = DEFAULT_REFRESH_TOTAL_BYTES

    def __post_init__(self):
        if type(self.max_files) is not int or not 1 <= self.max_files <= MAX_REFRESH_FILES:
            raise ValueError("知识刷新文件数量预算无效")
        if type(self.max_total_bytes) is not int or not 1 <= self.max_total_bytes <= MAX_REFRESH_TOTAL_BYTES:
            raise ValueError("知识刷新总字节预算无效")
