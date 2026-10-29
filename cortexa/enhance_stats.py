"""增强/治理/写回失败观察面（P1-4：从 except:pass 静默到可见）。

设计：单一模块级单例，9 类增强面计数 + 最近错误摘要；/health 暴露 snapshot()。
语义：增强/治理/关系建边/RARE 写回失败都不阻断主流程（韧性红线），但必须可观测——
"声明即执法"：失败次数与原因在监控面可见，而不是 200 掩蔽一切。

用法：
    from .enhance_stats import stats
    except Exception as _e:
        stats.inc("rare", err=_e)   # 增强非必需，但计数+留痕
"""

from __future__ import annotations

import threading


class EnhanceStats:
    """进程级观察单例（线程安全）。"""

    _KINDS = (
        "governance",      # 写入期治理（AML 维度 D supersede）
        "governance_skip", # supersede 失败改 kept 的业务分支
        "rare",            # RARE-lite 写回
        "enhance",         # 异步增强单条失败
        "enhance_spawn",   # 增强线程启动失败
        "graph",           # 关系建边/实体图写入失败
        "floor_check",     # 查体地板复验行解析失败
        "deadline_skip",   # A-2：Search 超 deadline 后跳过的加料步次数（降级可观测）
        "return_trim",     # A-3：返回预算自剪尾部的次数（官方 Q18 同向）
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {k: 0 for k in self._KINDS}
        self._last_error: dict[str, str] = {}

    def inc(self, kind: str, err: BaseException | None = None) -> None:
        """登记一次增强面失败。未知 kind 也记录（防御）。"""
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            if err is not None:
                msg = str(err).strip().replace("\n", " ")[:120]
                self._last_error[kind] = f"{type(err).__name__}: {msg}"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counts": dict(self._counts),
                "total": sum(self._counts.values()),
                "last_error": dict(self._last_error),
            }

    def reset(self) -> None:
        with self._lock:
            self._counts = {k: 0 for k in self._KINDS}
            self._last_error = {}


stats = EnhanceStats()
