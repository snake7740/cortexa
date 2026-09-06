# -*- coding: utf-8 -*-
"""Sandbox compat: DSH 沙箱对 mkdir(mode=0o700) 的目录拒绝 scandir(ACL 转换)。
sitecustomize 随 site 初始化自动加载；由 _run_pytest.py 经 PYTHONPATH 注入，
覆盖包括 subprocess 子进程在内的所有 python 进程。"""
import os as _os

_orig_mkdir = _os.mkdir

def _safe_mkdir(path, mode=0o777, *, dir_fd=None):
    if mode == 0o700:
        mode = 0o755
    return _orig_mkdir(path, mode, dir_fd=dir_fd)

if getattr(_os, "mkdir", None) is not _safe_mkdir:
    _os.mkdir = _safe_mkdir
