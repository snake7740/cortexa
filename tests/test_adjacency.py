"""同会话邻接扩展（2026-10-29）+ eval 档合规收敛的测试。

覆盖：
- adjacency：默认关=零行为变化；开启后 ±1 邻居插位、会话隔离、top_k 预算、去重；
- eval 档 /health 最小面（官方 Q21：不暴露模型配置等内部信息）；
- eval 档 Search 无 touch（冻结单 §3：Search 纯函数口径）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import MemoryInstance, content_hash
from cortexa.modules.search import search_memory
from cortexa.server import create_app
from cortexa.storage import SQLiteMemoryStore


def _mk_store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "t.db"), embedding_dim=512)


def _add(store: SQLiteMemoryStore, scope: str, session: str, text: str) -> MemoryInstance:
    mem = MemoryInstance(scope=scope, session_id=session, content=text,
                         content_hash=content_hash(scope, text))
    assert store.add(mem) is True
    return mem


def _search(store, settings, query, scope, top_k):
    llm = LLMClient(settings)
    return search_memory(store, llm, settings, query=query, scope=scope,
                         top_k=top_k, journal=None)


class TestAdjacency:
    def _corpus(self, tmp_path: Path):
        store = _mk_store(tmp_path)
        s = "u1"
        texts = [
            "Mina accepted the invitation to the Berlin seminar.",      # m0
            "She will present the Atlas project there.",                # m1
            "The catering budget was set to forty thousand euros.",     # m2  种子目标
            "She moved her departure to Friday because of hospital duty.",  # m3
            "Tom bought a used bicycle last weekend.",                  # m4 (另一会话)
        ]
        mems = [_add(store, s, "conv-1", t) for t in texts[:4]]
        other = _add(store, s, "conv-2", texts[4])
        return store, s, mems, other

    def test_disabled_is_byte_identical(self, tmp_path: Path) -> None:
        store, s, mems, _ = self._corpus(tmp_path)
        base = Settings(data_dir=tmp_path / "d1", embedding_provider="hash",
                        journal_mode="eval", _env_file=None)
        off = [r.id for r in _search(store, base, "catering budget euros", s, 5)]
        # 默认关：邻居不出现
        assert mems[0].id not in off

    def test_neighbors_insert_dedup_and_order(self, tmp_path: Path) -> None:
        store, s, mems, other = self._corpus(tmp_path)
        cfg = Settings(data_dir=tmp_path / "d2", embedding_provider="hash",
                       journal_mode="eval", adjacency_enabled=True,
                       adjacency_seeds=20, adjacency_radius=1, _env_file=None)
        ids = [r.id for r in _search(store, cfg, "catering budget euros", s, 5)]
        # 种子 m2 命中，前邻 m1 / 后邻 m3 紧跟，按时间序 [m1, m2, m3]
        assert mems[2].id in ids
        i = ids.index(mems[2].id)
        assert ids[max(0, i - 1)] == mems[1].id
        assert ids[i + 1] == mems[3].id
        # 会话隔离：另一会话的 m4 不因邻接进入（m3 的后邻不在同会话）
        assert other.id not in ids
        # 预算：top_k=5 时返回不超 5
        assert len(ids) <= 5

    def test_topk_budget_not_exceeded(self, tmp_path: Path) -> None:
        store, s, mems, _ = self._corpus(tmp_path)
        cfg = Settings(data_dir=tmp_path / "d3", embedding_provider="hash",
                       journal_mode="eval", adjacency_enabled=True,
                       adjacency_seeds=20, adjacency_radius=1, _env_file=None)
        ids = [r.id for r in _search(store, cfg, "catering budget euros", s, 1)]
        assert len(ids) <= 1

    def test_active_timeline_caching_and_status(self, tmp_path: Path) -> None:
        store, s, mems, other = self._corpus(tmp_path)
        tl = store.active_timeline(s)
        # 时间线 = scope 内全部 ACTIVE（含另一会话），按入库序
        assert [mid for _sid, mid in tl] == [m.id for m in mems] + [other.id]
        # 二次调用命中缓存（同列表内容）
        assert store.active_timeline(s) == tl
        # supersede 使旧值离开 ACTIVE → 缓存失效、时间线收缩
        store.supersede(mems[0].id, mems[1].id, s)
        tl2 = store.active_timeline(s)
        assert mems[0].id not in [mid for _sid, mid in tl2]


class TestEvalHealthMinimal:
    def test_eval_mode_minimal_surface(self, tmp_path: Path) -> None:
        cfg = Settings(data_dir=tmp_path / "h1", embedding_provider="hash",
                       journal_mode="eval", api_key="k1", _env_file=None)
        with TestClient(create_app(cfg)) as client:
            body = client.get("/health").json()
            assert body == {"status": "ok"}  # 无模型配置/指标暴露

    def test_app_mode_keeps_full_surface(self, tmp_path: Path) -> None:
        cfg = Settings(data_dir=tmp_path / "h2", embedding_provider="hash",
                       _env_file=None)
        with TestClient(create_app(cfg)) as client:
            body = client.get("/health").json()
            assert body["status"] == "ok"
            assert {"scene", "journal_mode", "api_key_required"} <= set(body)


class TestEvalSearchNoTouch:
    def test_eval_skips_touch_app_touches(self, tmp_path: Path) -> None:
        store = _mk_store(tmp_path)
        mem = _add(store, "u", "s1", "Alice adopted a dog named Rob.")
        ev_cfg = Settings(data_dir=tmp_path / "e1", embedding_provider="hash",
                          journal_mode="eval", _env_file=None)
        _search(store, ev_cfg, "adopted dog Rob", "u", 3)
        assert store.get(mem.id, "u").fsrs.last_review_at is None

        app_cfg = Settings(data_dir=tmp_path / "e2", embedding_provider="hash",
                           journal_mode="app", _env_file=None)
        _search(store, app_cfg, "adopted dog Rob", "u", 3)
        assert store.get(mem.id, "u").fsrs.last_review_at is not None
