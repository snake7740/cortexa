"""pytest 全局夹具：环境隔离。

1) 真实机器上的 OPENAI_API_KEY / OPENAI_BASE_URL 会经 facade 的兑底通道渗入测试；
2) 仓库根 .env（用户的真实端点配置）会经 facade 的 cwd 自动探测渗入测试。
两者都会把测试切到 openai 嵌入 → 网络依赖 + 不确定性。测试口径必须是离线确定
的 hash 嵌入，故在此统一剥除环境变量并把 cwd 移出仓库根；需要云端行为的用例
自行显式构造参数/临时文件开启。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_cloud_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # 防仓库根 .env 经 cwd 探测渗入
