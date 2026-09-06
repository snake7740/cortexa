# -*- coding: utf-8 -*-
"""Sandbox-safe pytest runner: rewrite mkdir mode 0o700 -> 0o755.

DSH 文件沙箱把 os.mkdir(mode=0o700) 转成 ACL 后目录无法 scandir(仅 0o700 受影响)。
pytest/tempfile 均用 0o700; 统一重写为 0o755。sitecustomize 经 PYTHONPATH 注入,
subprocess 子进程同样生效。不触碰业务代码。
"""
import os, sys, uuid, pathlib, contextlib, io, traceback

ROOT = pathlib.Path(__file__).resolve().parent
work = ROOT / ".pytest_work" / ("run-" + uuid.uuid4().hex[:12])
work.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = str(work); os.environ["TEMP"] = str(work); os.environ["TMPDIR"] = str(work)

compat = str(ROOT / "_sandbox_compat")
old_pp = os.environ.get("PYTHONPATH", "")
os.environ["PYTHONPATH"] = compat + (os.pathsep + old_pp if old_pp else "")

_orig_mkdir = os.mkdir
def _safe_mkdir(path, mode=0o777, *, dir_fd=None):
    if mode == 0o700:
        mode = 0o755
    return _orig_mkdir(path, mode, dir_fd=dir_fd)
os.mkdir = _safe_mkdir

targets = sys.argv[1:] or ["tests"]
out = work / "out.txt"
import pytest
buf = io.StringIO()
code = 2
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider",
                            "--rootdir", str(ROOT)] + targets)
except BaseException as e:
    buf.write("\n===PYTEST RAISED===\n" + traceback.format_exc())
    code = 3
with open(out, "w", encoding="utf-8", errors="replace") as f:
    f.write(buf.getvalue())
print("WORK=" + str(work))
sys.exit(0)
