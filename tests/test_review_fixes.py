"""架构 review 修复的回归锁（2026-08-31 全面 review 产出的 A/B/D 级问题）。

每条测试对应一个"声明与实现脱节"缺陷，修复前必失败：
- A-1 openai 嵌入维度错配 -> embed() 必须显式传 dimensions（否则重启后语义通道静默瘫痪）
- A-2 clear_scope 不删 journal -> DELETE /scope 后 app 模式流水原文随之清除（合规红线）
- B-1 红线阈值无消费方 -> /health 公开 add/search within_budget 自证
- D-1 slice_at 固定行限额 -> 早章状态点不被截断
- D-2 journal events limit=None 全量扫描
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa import Cortexa
from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import MemoryInstance, MemoryType
from cortexa.modules.consistency import DEFAULT_SCALE, slice_at
from cortexa.modules.search import _retention_factor
from cortexa.server import create_app


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", embedding_provider="hash", _env_file=None)
    with TestClient(create_app(settings)) as c:
        yield c


class TestA1EmbeddingDimensions:
    """A-1：openai 嵌入必须显式降维到 embedding_dim，否则维度错配。"""

    def test_openai_embed_passes_dimensions(self) -> None:
        captured: dict = {}

        class FakeEmbeddings:
            def create(self, **kwargs):
                captured.update(kwargs)

                class D:
                    index = 0
                    embedding = [0.0] * kwargs.get("dimensions", 512)

                class R:
                    data = [D()]

                return R()

        settings = Settings(
            embedding_provider="openai", openai_api_key="sk-test",
            embedding_dim=256, _env_file=None,
        )
        llm = LLMClient(settings)
        llm._openai_client = type("C", (), {"embeddings": FakeEmbeddings()})()
        vecs = llm.embed(["hello world"])
        assert captured.get("dimensions") == 256, "openai 调用必须传 dimensions 对齐存储矩阵"
        assert len(vecs) == 1 and len(vecs[0]) == 256

    def test_non_matryoshka_model_skips_dimensions(self) -> None:
        captured: dict = {}

        class FakeEmbeddings:
            def create(self, **kwargs):
                captured.update(kwargs)

                class D:
                    index = 0
                    embedding = [0.0] * 1536

                class R:
                    data = [D()]

                return R()

        settings = Settings(
            embedding_provider="openai", openai_api_key="sk-test",
            embedding_model="text-embedding-ada-002", _env_file=None,
        )
        llm = LLMClient(settings)
        llm._openai_client = type("C", (), {"embeddings": FakeEmbeddings()})()
        llm.embed(["x"])
        assert "dimensions" not in captured, "非 3 系模型不支持 dimensions，不得传"


class TestA2ClearScopeJournal:
    """A-2：clear_scope 必须同步删该 scope 的 journal 行（评测数据不留日志红线）。"""

    def test_journal_rows_removed_with_scope(self, cm: Cortexa) -> None:
        cm.remember("甲scope的一条记忆", scope="keep")
        cm.remember("待删scope的一条记忆", scope="drop")
        conn = cm._store.connection
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE scope = 'drop'"
        ).fetchone()[0] > 0
        cm.forget_scope("drop")
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE scope = 'drop'"
        ).fetchone()[0] == 0
        # 其他 scope 流水不受波及
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE scope = 'keep'"
        ).fetchone()[0] > 0


class TestB1HealthBudgetFlags:
    """B-1：/health 必须把红线阈值与实测比对并公开（不能只声明不执法）。"""

    def test_health_exposes_within_budget(self, client: TestClient) -> None:
        client.post("/add", json={
            "request_id": "r1", "scope": "s",
            "messages": [{"role": "user", "content": "包管理器用 pnpm"}],
        })
        client.post("/search", json={"query": "包管理器", "scope": "s", "top_k": 5})
        body = client.get("/health").json()
        assert isinstance(body.get("add_within_budget"), bool)
        assert isinstance(body.get("search_within_budget"), bool)
        assert "add_p95_ms" in body
        assert body["add_within_budget"] is True  # 本机样本远低于 500ms 预算


class TestD1SliceAtNoTruncation:
    """D-1：早章唯一状态点不得因固定行限额被静默丢弃（长篇可复盘红线）。"""

    def test_early_state_point_survives_large_scope(self, cm: Cortexa) -> None:
        s = "big"
        # 主角唯一状态点落在最早的第 1 章
        cm.remember("【第1章·起源】叶长生的修为是炼气三层。",
                    scope=s, session_id="ch001",
                    timestamp=1_760_000_000_000 + 1 * 3_600_000)
        # 其后堆叠 450 条不含主角的活跃记忆（event_ts 均晚于第 1 章）
        for ch in range(2, 452):
            cm.remember(f"【第{ch}章·】众人议事，无关角色。",
                        scope=s, session_id=f"ch{ch:03d}",
                        timestamp=1_760_000_000_000 + ch * 3_600_000)
        points = slice_at(
            cm._store.connection, s, 500, scale=DEFAULT_SCALE, subject="叶长生"
        )
        assert points, "第 1 章主角状态点被截断丢失（LIMIT 未移除）"
        assert points[0].chapter == 1


class TestD2JournalUnboundedScan:
    """D-2：events(limit=None) 全量扫描；orphan_stats 默认无固定限额。"""

    def test_events_limit_none_returns_all(self, cm: Cortexa) -> None:
        s = "many"
        for i in range(5):
            cm.remember(f"记忆片段 number {i}，关于部署流程", scope=s)
        assert len(cm.journal.events("add", scope=s, limit=None)) == 5
        # 有限额时仍受约束（向后兼容）
        assert len(cm.journal.events("add", scope=s, limit=3)) == 3

    def test_orphan_stats_default_full_scan(self, cm: Cortexa) -> None:
        s = "orphan"
        cm.remember("唯一的一条设定记忆", scope=s)
        stats = cm.journal.orphan_stats(scope=s)
        assert stats["scan_limit"] is None  # 默认全量，非固定 500
        assert stats["active_memories"] == 1


class TestPagedVectorCache:
    """分页向量缓存（PagedAttention 思想消除冷启动摄取 O(N²)）的对齐不变量。

    行为保持型重构，测试锁的是新实现的风险点：跨页 fold 时 id/行一一对齐
    （空页过滤、截齐逻辑不得丢行或错位，否则 vector_search 会拿错 id）。
    """

    def test_fold_preserves_alignment_across_pages(self, cm: Cortexa) -> None:
        s = "paged"
        n = 600  # 跨 256 页边界（> 2 页）
        for i in range(n):
            cm.remember(f"设定条目编号 {i}：星图第七宿的位置记录", scope=s)
        assert cm._store._vec_matrix is None  # 尚未 fold（无检索发生）
        cm.recall("星图", scope=s, top_k=3)  # 触发分页折叠
        store = cm._store
        assert store._vec_matrix is not None
        assert store._vec_matrix.shape[0] == len(store._vec_ids) == n, "跨页 fold 行/ids 错位"
        assert store._vec_pages == []  # 页已全部折入

    def test_search_then_incremental_add_consistent(self, cm: Cortexa) -> None:
        s = "inc"
        cm.remember("初始记忆 星图第一宿", scope=s)
        cm.recall("星图", scope=s)  # 冻结
        for i in range(3):  # 冻结后追加走 vstack 兑底
            cm.remember(f"新增条目 {i} 关联星图第二宿", scope=s)
        cm.recall("新增条目", scope=s)
        store = cm._store
        assert store._vec_matrix.shape[0] == len(store._vec_ids) == 4


class TestFSRSRetentionModulation:
    """w3_fsrs_wire：R(t)×retrieval_weight 融合调制（首过中性 + RULE 守卫 + 因子有界）。

    修复前：retrievability()/retrieval_weight 已声明无消费方（声明与实现脱节）；
    直接乘 R(t) 会令未复习新记忆归零→首次评测退化。本锁钉死“首过恒等”不变量。
    """

    def test_unreviewed_factor_is_neutral(self, cm: Cortexa) -> None:
        # 未复习记忆 factor 必须恒等 1.0——首次评测零退化的根基
        mem = MemoryInstance(scope="s", content="x")
        assert mem.fsrs.last_review_at is None
        assert _retention_factor(mem, cm._settings) == 1.0

    def test_aged_plain_downweighted_but_bounded(self, cm: Cortexa) -> None:
        w = cm._settings.retrieval_fusion_weight
        mem = MemoryInstance(scope="s", content="x")
        mem.fsrs.last_review_at = time.time() - 400 * 86400  # 400 天未复习
        mem.fsrs.stability = 1.0
        f = _retention_factor(mem, cm._settings)
        assert 1.0 - w <= f < 1.0, "老化非 RULE 应被下调，但触底 1-w 绝不归零"

    def test_rule_and_no_decay_guarded(self, cm: Cortexa) -> None:
        aged = time.time() - 400 * 86400
        rule = MemoryInstance(scope="s", content="r", mem_type=MemoryType.RULE)
        rule.fsrs.last_review_at = aged
        rule.fsrs.stability = 1.0
        assert _retention_factor(rule, cm._settings) == 1.0  # 维度 F：规则不参与衰减
        nd = MemoryInstance(scope="s", content="n", no_decay=True)
        nd.fsrs.last_review_at = aged
        nd.fsrs.stability = 1.0
        assert _retention_factor(nd, cm._settings) == 1.0

    def test_factor_never_amplifies(self, cm: Cortexa) -> None:
        mem = MemoryInstance(scope="s", content="x")
        mem.fsrs.last_review_at = time.time()  # 刚复习 R=1
        assert _retention_factor(mem, cm._settings) <= 1.0  # 只下调不放大

    def test_retrieval_weight_is_consumed(self, cm: Cortexa) -> None:
        # retrieval_weight 不再是无消费方的空字段：同样复习时间下，权重越低因子越小
        aged = time.time() - 100 * 86400
        full = MemoryInstance(scope="s", content="x")
        full.fsrs.last_review_at = aged
        full.fsrs.stability = 5.0
        full.retrieval_weight = 1.0
        half = MemoryInstance(scope="s", content="y")
        half.fsrs.last_review_at = aged
        half.fsrs.stability = 5.0
        half.retrieval_weight = 0.5
        assert _retention_factor(half, cm._settings) < _retention_factor(full, cm._settings)

    def test_recall_works_on_fresh_store(self, cm: Cortexa) -> None:
        # 集成面：全新库首次 recall 与调制无涉（等价基线可命中）
        s = "fresh"
        cm.remember("Never deploy to production on Friday afternoon", scope=s)
        hits = cm.recall("deploy", scope=s, top_k=5)
        assert hits and hits[0].memory.startswith("Never deploy")


class TestFeatureCueChannel:
    """实体特征访问通道 + 空 scope 缺席闸。

    默认关闭（2026-08-31 审计：小说 A/B Δ=0 + 延迟为正，实际使用先检验原则）；
    本组测试显式开启以验证通道机制正确，开关回退/隔离/守卫行为不随默认值改变。
    """

    def test_entity_cue_recall_stable(self, cm: Cortexa) -> None:
        s = "cue"
        cm._settings.entity_channel = True
        cm.remember("ZephyrWidget9000 was adopted in the spring.", scope=s)
        # 查询 = 实体名 + 无重叠中文：实体通道与 FTS 双保险命中，
        # 锁住"特征线索稳定唤起"行为（开关关闭后同语义由开关用例验证）
        hits = cm.recall("ZephyrWidget9000 是什么东西", scope=s, top_k=5)
        assert hits and "ZephyrWidget9000" in hits[0].memory

    def test_multi_entity_match_ranks_higher(self, cm: Cortexa) -> None:
        s = "rank"
        cm._settings.entity_channel = True
        cm.remember("Alice met Bob at the conference.", scope=s)
        cm.remember("Alice went hiking alone.", scope=s)
        hits = cm.recall("what did Alice and Bob do", scope=s, top_k=5)
        assert hits[0].memory == "Alice met Bob at the conference."

    def test_single_char_entity_not_matched(self, cm: Cortexa) -> None:
        # 单字符实体不得参与匹配（否则 "a" 会误中一切含 a 的查询）
        s = "guard"
        mem = MemoryInstance(scope=s, content="nothing relevant here", entities=["a", "OK"])
        assert cm._store.add(mem)
        hits = cm.recall("a b c d e f g", scope=s, top_k=5)
        assert all(r.memory != "nothing relevant here" for r in hits), (
            "单字符实体 a 被误匹配（len>=2 守卫生效）"
        )

    def test_empty_scope_absence_gate(self, cm: Cortexa) -> None:
        # 缺席闸：空 scope 确定性返回空数组（合法且免嵌入开销），不抛异常；
        # app 模式 journal 仍留痕（审计链不断）
        hits = cm.recall("anything at all", scope="void-scope", top_k=5)
        assert hits == []
        assert len(cm.journal.events("search", scope="void-scope")) == 1

    def test_entity_channel_switch_off_not_called(self, cm: Cortexa) -> None:
        # 开关回退的硬证明：置 False 后检索不触碰实体通道（探子抛异常即失败），
        # 置 True 后探子被调用——开关真断开而不是形同虚设。
        s = "switchoff"
        cm.remember("ZephyrWidget9000 was adopted in the spring.", scope=s)
        def _tripwire(*_args, **_kwargs):
            raise AssertionError("entity_search 在开关关闭后仍被调用")

        original = cm._store.entity_search
        cm._store.entity_search = _tripwire
        cm._settings.entity_channel = False
        try:
            hits = cm.recall("ZephyrWidget9000", scope=s, top_k=5)
            assert isinstance(hits, list)  # 双通道基线契约形状不变（降序数组合法）
        finally:
            cm._store.entity_search = original
            cm._settings.entity_channel = True

        called = {"n": 0}

        def _spy(query, scope, limit):
            called["n"] += 1
            return original(query, scope, limit)

        cm._store.entity_search = _spy
        try:
            cm.recall("ZephyrWidget9000", scope=s, top_k=5)
        finally:
            cm._store.entity_search = original
        assert called["n"] == 1

    def test_entity_channel_respects_scope_isolation(self, cm: Cortexa) -> None:
        cm._settings.entity_channel = True
        cm.remember("ZephyrWidget9000 lives here.", scope="iso-a")
        hits = cm.recall("ZephyrWidget9000", scope="iso-b", top_k=5)
        assert hits == []  # 官方契约：检索隔离以 scope 为准，实体通道同样不得跨 scope


class TestChapterChannel:
    """编号精确通道（真实使用痛点：小说"第 107 章讲了什么"、代码工单号类编号检索）。

    章节锚点是摄取时打上的确定性前缀（【第 N 章·标题】），LIKE 前缀匹配零幻觉；
    FTS 对孤立数字/中文数字命中弱，本通道补位。开关可回退，无章节模式不触碰。
    """

    def test_arabic_chapter_recall(self, cm: Cortexa) -> None:
        s = "book"
        cm._store.add(MemoryInstance(scope=s, content="【第107章·决战】陈平挥出关键一剑。"))
        cm._store.add(MemoryInstance(scope=s, content="【第12章·入门】陈平拜入天枢宗。"))
        hits = cm.recall("第107章讲了什么", scope=s, top_k=3)
        assert hits and hits[0].memory.startswith("【第107章")

    def test_cn_numeral_chapter_recall(self, cm: Cortexa) -> None:
        s = "bookcn"
        cm._store.add(MemoryInstance(scope=s, content="【第37章·转折】苏晚晴身世揭露。"))
        cm._store.add(MemoryInstance(scope=s, content="【第3章·开局】叶长生测出废灵根。"))
        hits = cm.recall("第三十七章发生了什么", scope=s, top_k=3)
        assert hits and hits[0].memory.startswith("【第37章")

    def test_ch_prefix_recall(self, cm: Cortexa) -> None:
        s = "bookch"
        cm._store.add(MemoryInstance(scope=s, content="【Ch 40】陈平沉默了。"))
        hits = cm.recall("Ch 40 的剧情", scope=s, top_k=3)
        assert hits and hits[0].memory.startswith("【Ch 40")

    def test_extract_chapter_nos_unit(self, cm: Cortexa) -> None:
        from cortexa.modules.search import extract_chapter_nos

        assert extract_chapter_nos("第107章讲了什么") == [107]
        assert extract_chapter_nos("第三十七章与第12章对比") == [12, 37]
        assert extract_chapter_nos("第5章和第5章") == [5]  # 去重
        assert extract_chapter_nos("陈平的师父是谁") == []  # 无模式不触发
        assert extract_chapter_nos("第一百零七章") == [107]  # 中文复合数字
        assert extract_chapter_nos("ch40剧情") == [40]
        assert extract_chapter_nos("第零章") == []  # 非法章节号丢弃（_cn_numeral 下限 1）

    def test_switch_off_not_called(self, cm: Cortexa) -> None:
        s = "chswitch"
        cm._store.add(MemoryInstance(scope=s, content="【第5章·测试】内容。"))

        def _tripwire(*_args, **_kwargs):
            raise AssertionError("chapter_search 在开关关闭后仍被调用")

        original = cm._store.chapter_search
        cm._store.chapter_search = _tripwire
        cm._settings.chapter_channel = False
        try:
            assert isinstance(cm.recall("第5章讲了什么", scope=s, top_k=3), list)
        finally:
            cm._store.chapter_search = original

    def test_no_chapter_pattern_not_called(self, cm: Cortexa) -> None:
        # 查询无章节模式 → 不得触碰通道（避免无谓扫描）
        s = "chpat"
        cm._store.add(MemoryInstance(scope=s, content="【第5章·测试】内容。"))
        called = {"n": 0}
        original = cm._store.chapter_search

        def _spy(nos, scope, limit):
            called["n"] += 1
            return original(nos, scope, limit)

        cm._store.chapter_search = _spy
        try:
            cm.recall("陈平的师父是谁", scope=s, top_k=3)
        finally:
            cm._store.chapter_search = original
        assert called["n"] == 0

    def test_chapter_scope_isolation(self, cm: Cortexa) -> None:
        cm._store.add(MemoryInstance(scope="ch-iso-a", content="【第5章·测试】内容。"))
        hits = cm.recall("第5章讲了什么", scope="ch-iso-b", top_k=3)
        assert hits == []  # 章节锚点检索同样受 scope 隔离约束
