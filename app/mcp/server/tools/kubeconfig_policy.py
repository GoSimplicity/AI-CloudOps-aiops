#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI-CloudOps-aiops
Author: Bamboo
Email: bamboocloudops@gmail.com
License: Apache 2.0
Description: SEC-46：自定义 kubeconfig（config_path）准入策略

- 只接受 mcp.kubeconfig_allowed_dir（MCP_KUBECONFIG_ALLOWED_DIR）白名单目录内的文件，默认空 = 禁用自定义路径；
- 白名单目录按 realpath 判断，若与 data/ 或知识库目录重叠（位于其下或包含它们）视为无效；
- 文件按 realpath 判断必须位于白名单目录内，O_NOFOLLOW 打开、必须是硬链接数为 1 的普通文件、不超过 1 MiB；
- 加载前先解析：users[].user 含 exec / auth-provider 一律拒绝（二者都会在加载时执行本地命令）；
- 校验与加载使用同一份已解析的 dict，不再按路径二次读取。
所有拒绝原因对调用方返回同一条固定文案，避免成为文件存在性/内容探测的 oracle。
"""

import logging
import os
import stat
from typing import Any, Dict, Optional, Tuple

import yaml

logger = logging.getLogger("aiops.mcp.k8s")

CONFIG_PATH_UNAVAILABLE_MESSAGE = "config_path 不可用（未启用自定义 kubeconfig、不在白名单目录内或不是允许的只读 kubeconfig），未执行任何操作"
MAX_KUBECONFIG_BYTES = 1024 * 1024
# 加载时会执行本地命令的凭据插件：exec（client-go 插件）与 auth-provider（gcp cmd-path 等）
FORBIDDEN_USER_KEYS = frozenset({"exec", "auth-provider"})


class KubeconfigRejected(ValueError):
    """config_path 未通过准入；消息固定为 CONFIG_PATH_UNAVAILABLE_MESSAGE"""

    def __init__(self, reason: str):
        super().__init__(CONFIG_PATH_UNAVAILABLE_MESSAGE)
        # 内部原因只进日志（不含路径与文件内容）
        self.reason = reason


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _forbidden_roots() -> Tuple[str, ...]:
    """platform 与 mcp 共享的 data/ 及知识库目录（上传文件落点），按 realpath"""
    from app.config.settings import ROOT_DIR, config

    candidates = [os.path.join(os.getcwd(), "data"), str(ROOT_DIR / "data")]
    knowledge = getattr(getattr(config, "rag", None), "knowledge_base_path", None)
    if isinstance(knowledge, str) and knowledge.strip():
        candidates.append(knowledge if os.path.isabs(knowledge) else os.path.join(os.getcwd(), knowledge))
        if not os.path.isabs(knowledge):
            candidates.append(str(ROOT_DIR / knowledge))
    return tuple(dict.fromkeys(os.path.realpath(path) for path in candidates))


def allowed_kubeconfig_dir() -> Optional[str]:
    """返回有效白名单目录的 realpath；未配置或配置无效时返回 None（= 禁用自定义 config_path）"""
    from app.config.settings import config

    value = getattr(config.mcp, "kubeconfig_allowed_dir", "")
    if not isinstance(value, str) or not value.strip():
        return None
    real = os.path.realpath(value.strip())
    if real == os.sep or not os.path.isdir(real):
        logger.warning("mcp.kubeconfig_allowed_dir 无效（不存在、不是目录或为根目录），自定义 config_path 已禁用")
        return None
    for forbidden in _forbidden_roots():
        if _is_within(real, forbidden) or _is_within(forbidden, real):
            logger.warning("mcp.kubeconfig_allowed_dir 与 data/ 或知识库目录重叠，自定义 config_path 已禁用")
            return None
    return real


def _reject_credential_plugins(document: Any) -> None:
    if not isinstance(document, dict):
        raise KubeconfigRejected("kubeconfig 顶层不是映射")
    users = document.get("users")
    if users is None:
        return
    if not isinstance(users, list):
        raise KubeconfigRejected("users 不是列表")
    for entry in users:
        if not isinstance(entry, dict):
            raise KubeconfigRejected("users 条目不是映射")
        user = entry.get("user")
        if user is None:
            continue
        if not isinstance(user, dict):
            raise KubeconfigRejected("users[].user 不是映射")
        for key in user:
            if not isinstance(key, str) or key.strip().lower() in FORBIDDEN_USER_KEYS:
                raise KubeconfigRejected("kubeconfig 含 exec/auth-provider 凭据插件")


def _read_regular_file(path: str) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise KubeconfigRejected("平台不支持 O_NOFOLLOW")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise KubeconfigRejected("无法打开 kubeconfig") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KubeconfigRejected("kubeconfig 不是普通文件")
        if info.st_nlink != 1:
            raise KubeconfigRejected("kubeconfig 存在硬链接")
        if info.st_size > MAX_KUBECONFIG_BYTES:
            raise KubeconfigRejected("kubeconfig 超过大小上限")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_KUBECONFIG_BYTES:
                raise KubeconfigRejected("kubeconfig 超过大小上限")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_custom_kubeconfig(config_path: Any) -> Tuple[str, Dict[str, Any]]:
    """校验 config_path 并返回 (realpath, 已解析且已通过检查的 kubeconfig dict)；任何不满足都抛 KubeconfigRejected"""
    if not isinstance(config_path, str) or not config_path.strip():
        raise KubeconfigRejected("config_path 不是非空字符串")
    allowed = allowed_kubeconfig_dir()
    if allowed is None:
        raise KubeconfigRejected("未启用自定义 kubeconfig 白名单目录")
    candidate = config_path if os.path.isabs(config_path) else os.path.join(allowed, config_path)
    real = os.path.realpath(candidate)
    if real == allowed or not _is_within(real, allowed):
        raise KubeconfigRejected("config_path 不在白名单目录内")
    if any(_is_within(real, forbidden) for forbidden in _forbidden_roots()):
        raise KubeconfigRejected("config_path 位于 data/ 或知识库目录")
    raw = _read_regular_file(real)
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise KubeconfigRejected("kubeconfig 不是合法的 UTF-8 YAML") from None
    _reject_credential_plugins(document)
    return real, document
