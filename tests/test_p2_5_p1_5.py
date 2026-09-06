"""P2-5 + P1-5 测试：守卫警告纯度 / Enhancer 场景一致 / 增强线程 drain。

P2-5：guard_lifecycle 不再污染 content（证据原文纯度），警告进 SearchResult.warnings
      且序列化排除（官方 data 元素字段不变）。
P1-5b：Enhancer schema 从 settings.scene 解析（AML 场景不再拿 novel schema 兜底）。
P1-5a：增强线程可登记并可被 wait_enhance_threads 优雅收尾。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cortexa.config import Settings
from cortexa.enhancer import Enhancer, track_enhance_thread, wait_enhance_threads
from cortexa.models import LifecycleStatus, SearchResult
from cortexa.modules.novel_adapter import NovelAdapter
from cortexa.schema import get_schema
from cortexa.storage import SQLiteMemoryStore


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "p.db"), embedding_dim=8)


def _settings(tmp_path: Path, scene: str = "novel") -> Settings:
    return Settings(data_dir=tmp_path, embedding_provider="hash", scene=scene, _env_file=None)


def test_guard_warnings_do_not_pollute_content(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    adapter = NovelAdapter(store, None, _settings(tmp_path))
    # 直接伪造一个 EXITED 实体，避免依赖抽取
    fake_exited = [("赵铁柱", LifecycleStatus.EXITED, 16)]
    monkeypatch.setattr(adapter, "_get_exited_entities", lambda scope: fake_exited)
    results = [SearchResult(id="m1", content="赵铁柱死后的江湖传闻还在扩散")]
    guarded = adapter.guard_lifecycle(results, "s")
    assert guarded[0].content == results[0].content  # 证据原文不被追加警告
    assert guarded[0].warnings and "⚠️" in guarded[0].warnings[0]
    # 序列化排除：官方 data 元素不应出现 warnings 键
    dumped = guarded[0].model_dump()
    assert "warnings" not in dumped
    assert dumped["content"] == results[0].content


def test_enhancer_uses_scene_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    enh = Enhancer(store, None, _settings(tmp_path, scene="aml"))
    assert enh._schema is get_schema("aml")
    enh2 = Enhancer(store, None, _settings(tmp_path, scene="novel"))
    assert enh2._schema is get_schema("novel")


def test_enhance_threads_drain(tmp_path: Path) -> None:
    done = threading.Event()

    def work():
        time.sleep(0.05)
        done.set()

    t = threading.Thread(target=work, daemon=True)
    track_enhance_thread(t)
    t.start()
    alive = wait_enhance_threads(timeout_s=3.0)
    assert alive == 0
    assert done.is_set()
    # 无登记线程时直接返回 0（幂等）
    assert wait_enhance_threads() == 0


def test_facade_close_drains_enhance(tmp_path: Path) -> None:
    """真实 run_add（离线 hash、无 LLM key）后 close 不抛异常且线程被 drain。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cortexa import Cortexa

    data = tmp_path / "d"
    data.mkdir()
    cm = Cortexa(data_dir=data, embedding_provider="hash", config_file=None)
    cm.remember("规则：新服务默认用 pnpm", scope="s", session_id="t1")
    # 增强线程已登记；close 触发 drain（不抛、不挂起）
    cm.close()
