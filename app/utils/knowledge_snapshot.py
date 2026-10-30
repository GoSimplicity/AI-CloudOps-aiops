"""有界、纯只读的知识目录快照；供异步刷新在线程内预检与复核。"""

from dataclasses import dataclass
import os
import stat
from threading import Event

from app.common.knowledge_budget import MAX_DIRECTORY_ENTRIES, MAX_KNOWLEDGE_FILE_BYTES, KnowledgeRefreshBudget
from app.utils.knowledge_directory import open_knowledge_directory, verify_knowledge_directory


@dataclass(frozen=True)
class KnowledgeFileSnapshot:
    name: str
    path: str
    content: str
    identity: tuple


@dataclass(frozen=True)
class KnowledgeSnapshot:
    directory: str
    resolved: str
    directory_identity: tuple
    files: tuple[KnowledgeFileSnapshot, ...]
    budget: KnowledgeRefreshBudget


def _check_stopped(stop: Event | None):
    if stop is not None and stop.is_set():
        raise RuntimeError("知识文件只读预检已取消")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)


def _knowledge_names(directory_fd: int, budget: KnowledgeRefreshBudget, stop: Event | None):
    names = []
    count = 0
    # scandir逐项限制目录条目，避免先listdir整个无界目录再检查上限。
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            _check_stopped(stop)
            count += 1
            if count > MAX_DIRECTORY_ENTRIES:
                raise ValueError("知识目录条目超过安全上限")
            if entry.name.endswith((".md", ".txt")):
                entry.name.encode("utf-8")  # 无法安全表示的文件名在任何发布前拒绝。
                names.append(entry.name)
                if len(names) > budget.max_files:
                    raise ValueError("知识刷新文件数量超过预算")
    return sorted(names)


def read_knowledge_snapshot(directory: str, budget: KnowledgeRefreshBudget, stop: Event | None = None) -> KnowledgeSnapshot:
    """无网络、模型或Redis对象；取消后后台线程也只有只读文件操作。"""
    _check_stopped(stop)
    directory = os.fspath(directory)
    directory_fd, resolved = open_knowledge_directory(directory)
    try:
        directory_info = os.fstat(directory_fd)
        names = _knowledge_names(directory_fd, budget, stop)
        files = []
        total_bytes = 0
        for name in names:
            _check_stopped(stop)
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("知识库包含不安全文件")
            if before.st_size > MAX_KNOWLEDGE_FILE_BYTES:
                raise ValueError("知识文件超过大小上限")
            if total_bytes + before.st_size > budget.max_total_bytes:
                raise ValueError("知识刷新总字节超过预算")
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if _identity(opened) != _identity(before):
                    raise ValueError("知识文件发生变化")
                data = bytearray()
                limit = min(MAX_KNOWLEDGE_FILE_BYTES, budget.max_total_bytes - total_bytes)
                while len(data) <= limit:
                    _check_stopped(stop)
                    chunk = os.read(file_fd, min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                _check_stopped(stop)
                after = os.fstat(file_fd)
                named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if len(data) > limit or _identity(after) != _identity(opened) or _identity(named) != _identity(opened) or len(data) != opened.st_size:
                    raise ValueError("知识文件读取期间发生变化或超过预算")
                total_bytes += len(data)
                files.append(KnowledgeFileSnapshot(name, os.path.join(directory, name), data.decode("utf-8"), _identity(after)))
            finally:
                os.close(file_fd)
        verify_knowledge_directory(directory, resolved, directory_fd)
        # 检查读取期间新增/删除的受支持文件；空文件同样纳入快照身份。
        if _knowledge_names(directory_fd, budget, stop) != names:
            raise ValueError("知识目录文件清单发生变化")
        _check_stopped(stop)
        return KnowledgeSnapshot(directory, resolved, (directory_info.st_dev, directory_info.st_ino), tuple(files), budget)
    finally:
        os.close(directory_fd)


def verify_knowledge_snapshot(snapshot: KnowledgeSnapshot, stop: Event | None = None) -> None:
    """stage采样revision后复核身份，拒绝用较早文件快照覆盖并发上传。"""
    _check_stopped(stop)
    directory_fd, resolved = open_knowledge_directory(snapshot.directory)
    try:
        opened = os.fstat(directory_fd)
        if resolved != snapshot.resolved or (opened.st_dev, opened.st_ino) != snapshot.directory_identity:
            raise ValueError("知识目录身份发生变化")
        if _knowledge_names(directory_fd, snapshot.budget, stop) != [file.name for file in snapshot.files]:
            raise ValueError("知识目录文件清单发生变化")
        for file in snapshot.files:
            _check_stopped(stop)
            current = os.stat(file.name, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(current) != file.identity:
                raise ValueError("知识文件在预检后发生变化")
        verify_knowledge_directory(snapshot.directory, resolved, directory_fd)
        _check_stopped(stop)
    finally:
        os.close(directory_fd)
