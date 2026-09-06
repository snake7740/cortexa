"""Cortexa — 仿生 AI 记忆系统（AML 2026 第二期参赛系统）。

两种使用形态，共用同一条管线（双入口单管线）：
- 类库：``from cortexa import Cortexa``（进程内，见 facade.py）
- 服务：``uvicorn cortexa.server:app``（HTTP，AML 官方协议契约面）

八层仿生管线按三条执行通道组织：
- 写入通道：L1 感知 -> L2 过滤 -> L3 编码 -> L4 入图（Add，全程同步）
- 检索通道：双通道召回 -> 融合 -> precision-first 截断（Search）
- 离线通道：巩固调度 / Dreaming / 遗忘（按评测诊断排期接入）
"""

__version__ = "0.1.0"

from .facade import Cortexa

__all__ = ["Cortexa", "__version__"]
