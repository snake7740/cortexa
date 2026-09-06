"""小说实体关系图回填（Palantir Link type 改造应用，2026-08-31，幂等）。

对《废灵根修仙-重构版》回填关键实体连边：只回填正文已确证的关系
（precision-first：传闻/未查实的一律不连，如"沈渊孟知秋同族"）；
每条边带 evidence_chapter（出处章），多语义用多 link_type（同两实体
可并存多条语义独立的边）。重复运行不产生重复边。

用法：
    python examples/novel_links.py <BOOK_ROOT> [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.pop("OPENAI_API_KEY", None)  # 本地零 API

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

from cortexa import Cortexa

# 基数声明：数字 1 标记受限端（该端每个实体至多一条活跃边）
LINK_TYPES = {
    "亲生父亲": "1-m",   # 一人至多一个亲生父亲
    "曾祖母": "m-1",     # link(曾祖母, 后人)：后人端受限（一人至多一位曾祖母）
    "传承": "m-m",
    "同行": "m-m",
    "因果羁绊": "m-m",
    "分裂自": "m-m",
    "同门": "m-m",
}

# (src, dst, link_type, evidence, evidence_chapter) —— 全部有正文出处
EDGES = [
    ("叶长生", "叶巨川", "亲生父亲", "死人册注脚：化名常生、惯吹木叶，父子相认线", 117),
    ("孟知秋", "苏晚晴", "曾祖母", "孟家一脉传物到她手上；苏晚晴亲口'曾祖母是那时候走的'", 112),
    ("孟知秋", "苏晚晴", "传承", "孟家三样遗物（虎符一半/口信/遗物）+天机阁旧派最后传人", 112),
    ("沈渊", "叶长生", "传承", "残魂授命数经、古虚戒与因果之眼承自沈渊", 73),
    ("沈渊", "孟知秋", "同门", "恒远：沈渊带的孟知秋入宗，传言师兄弟/结义兄弟", 32),
    ("叶长生", "苏晚晴", "同行", "结伴北上赴苍澜域北寻碑", 109),
    ("叶长生", "苏晚晴", "因果羁绊", "苏晚晴把虎符一半放入叶长生手心；折寿账与信物账", 112),
    ("旧派", "天机阁", "分裂自", "天机阁内部派系：旧派当年从阁里分出来", 105),
]


def _exists(cm: Cortexa, scope: str, src: str, dst: str, link_type: str) -> bool:
    for other, row in cm.neighbors(scope, src, link_type=link_type):
        if other == dst and row.status == "ACTIVE":
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="小说实体关系图回填（幂等）")
    ap.add_argument("root", help="小说项目根目录（BOOK_ROOT）")
    ap.add_argument("--dry-run", action="store_true", help="只报告差异，不写库")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    db = root / ".cortexa" / "cortexa.db"
    if not db.is_file():
        print(f"记忆库不存在：{db}（先跑 novel_bridge.py sync）")
        return 1
    scope = root.name

    with Cortexa(data_dir=root / ".cortexa") as cm:
        for name, card in LINK_TYPES.items():
            cm.declare_link_type(scope, name, card)
        added = skipped = 0
        for src, dst, lt, evidence, ch in EDGES:
            if _exists(cm, scope, src, dst, lt):
                print(f"= [已有] {src} --{lt}--> {dst}")
                skipped += 1
                continue
            if args.dry_run:
                print(f"+ [待加] {src} --{lt}--> {dst}（Ch{ch}）")
            else:
                cm.link(scope, src, dst, lt, evidence=f"Ch{ch} {evidence}",
                        evidence_chapter=ch)
                print(f"+ [已加] {src} --{lt}--> {dst}（Ch{ch} {evidence}）")
            added += 1
        print(f"\n类型 {len(LINK_TYPES)} 种；新增 {added}，已有跳过 {skipped}。")
        # 回填后双向遍历抽查
        for ent in ("叶长生", "苏晚晴", "孟知秋"):
            ns = cm.neighbors(scope, ent)
            print(f"{ent} 活跃连边 {len(ns)}：")
            for other, row in ns:
                print(f"  - {row.link_type} -> {other}（{row.evidence}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
