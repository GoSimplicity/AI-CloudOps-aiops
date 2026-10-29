"""固定可信配置的知识目录，兼容根目录软链接并检测运行中替换。"""
import os


def verify_knowledge_directory(directory: str, resolved: str, directory_fd: int) -> None:
    """配置路径与已打开目录必须仍指向同一目录对象"""
    opened = os.fstat(directory_fd)
    current = os.stat(directory)
    resolved_stat = os.stat(resolved, follow_symlinks=False)
    if (os.path.realpath(directory) != resolved
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or (resolved_stat.st_dev, resolved_stat.st_ino) != (opened.st_dev, opened.st_ino)):
        raise RuntimeError("知识目录发生变化")


def open_knowledge_directory(directory: str):
    """解析可信配置的根链接并以O_NOFOLLOW固定实际目录"""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("当前平台不支持安全知识目录")
    resolved = os.path.realpath(directory)
    flags = os.O_RDONLY | os.O_DIRECTORY | nofollow | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(resolved, flags)
    try:
        verify_knowledge_directory(directory, resolved, fd)
    except BaseException:
        os.close(fd)
        raise
    return fd, resolved
