"""聚合质量闸：compileall + ruff + pytest 一条命令（P1-7）。

用法：
    python scripts/check.py            # 全量
    python scripts/check.py --quick    # 跳过 ruff（无 ruff 环境）
返回码：0=全绿；1=任一环节失败。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(cmd: list[str]) -> int:
    print(f">> {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    quick = "--quick" in sys.argv
    codes: list[tuple[str, int]] = []
    codes.append(("compileall", _run([PY, "-m", "compileall", "-q", "cortexa", "examples", "tests", "scripts"])))
    if not quick:
        import shutil
        if shutil.which("ruff") or (Path(ROOT) / ".venv/Scripts/ruff.exe").exists():
            ruff = str(ROOT / ".venv/Scripts/ruff.exe") if (Path(ROOT) / ".venv/Scripts/ruff.exe").exists() else "ruff"
            codes.append(("ruff", _run([ruff, "check", "cortexa", "examples", "tests", "scripts"])))
        else:
            print(">> ruff 未安装，跳过（--quick 同义）")
    codes.append(("pytest", _run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"])))
    failed = [name for name, c in codes if c != 0]
    if failed:
        print(f"CHECK FAILED: {', '.join(failed)}")
        return 1
    print("CHECK OK: compileall + " + ("ruff + " if not quick else "") + "pytest 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
