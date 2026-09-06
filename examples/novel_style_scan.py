"""全书文风扫描（小说正文直接读文件，不依赖记忆库）。

定位：创作闭环的文风体检工具——回答"这本书现在的文风是什么、哪里变了"。
与 novel_adapter.StyleProfiler（单章写库版）互补：本脚本直接读正文章节原文，
输出可量化的全书文风画像 + 分卷趋势 + 文风漂移检测，供写作框架定调参考。

2026-09-02 三方联调实证（废灵根 208 章）：
- 正文引号存在风格断层（Ch1-76 全角弯引号 / Ch77+ 部分 ASCII 直引号等多次切换），
  扫描器对两种引号均兼容，且单独报告切换点——这是真实的文风一致性信号；
- 句长与对话占比随剧情推进显著变化（Ch141+ 对话占比翻倍），
  属"写作节奏自然演进"还是"风格漂移"由调用方（作者）判断，本工具只报数据。

用法：
    python examples/novel_style_scan.py <小说根目录> [--bucket 20] [--out cortexa-out]

输出：
    stdout 画像摘要 + <root>/cortexa-out/style_scan_<date>.md 全量报告
    （归位约定同 novel_bridge：报告落小说项目 cortexa-out/，正文与 .webnovel 不动）

退出码：0=正常完成；1=章节目录缺失或零章可分析。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

# 章节目录候选（与 novel_ingest/novel_wordcount 同约定）
_CHAPTER_DIR_CANDIDATES = ("废灵根", "正文", "chapters")

# 修辞标记词（纯统计口径，非文学评价）
_RHETORIC = re.compile(
    r"仿佛|宛如|犹如|好似|恰似|如同|一般|似的|一样"
    r"|骤然|猛然|赫然|陡然|倏然|蓦地|不禁|忍不住"
)

# 句子切分
_SENT_SPLIT = re.compile(r"[。！？!?…]+")

# 对话引号：全角弯引号（“”）与 ASCII 直引号（"）都算；「」兼容
_QUOTE_OPEN = frozenset(('"', "\u201c", "\u300c", "「"))
_QUOTE_PAIR = re.compile(
    r'["\u201c\u300c「]([^"\u201c\u201d\u300c\u300d「」]{1,300})["\u201d\u300d」]'
)


def clean_chapter(text: str) -> str:
    """去标题/空行/章节前缀，返回正文纯文本（保留空行作段落边界）。"""
    body: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            body.append("")
            continue
        if s.startswith("#"):
            continue
        if re.match(r"^第\d+章\s", s):  # 无 # 的标题行
            continue
        s = re.sub(r"^【第[^】]*章[^】]*】", "", s)
        body.append(s)
    return "\n".join(body)


def is_dialogue_line(ln: str) -> bool:
    s = ln.strip()
    return bool(s) and s[0] in _QUOTE_OPEN


def profile_one(text: str) -> dict:
    """单章文风指标（全部确定性统计，零 LLM）。"""
    cleaned = clean_chapter(text)
    chars = len(cleaned)
    if chars < 200:
        return {"chars": chars, "skip": True}

    sents = [
        s.strip()
        for s in _SENT_SPLIT.split(cleaned)
        if s.strip() and len(s.strip()) > 1
    ]
    paras = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    speech_chars = sum(len(m) for m in _QUOTE_PAIR.findall(cleaned))

    return {
        "chars": chars,
        "skip": False,
        "avg_sent": statistics.mean(len(s) for s in sents) if sents else 0.0,
        "avg_para": statistics.mean(len(p) for p in paras) if paras else 0.0,
        "dlg_line_ratio": sum(1 for ln in lines if is_dialogue_line(ln)) / max(len(lines), 1),
        "speech_ratio": speech_chars / max(chars, 1),
        "rhet": len(_RHETORIC.findall(cleaned)) / max(chars, 1),
    }


def quote_style(text: str) -> str:
    """判定章节主导引号风格（curly=全角弯引号 / ascii=直引号）。"""
    ascii_n = text.count('"')
    curly_n = text.count("\u201c")
    return "curly" if curly_n > ascii_n else ("ascii" if ascii_n else "none")


def scan(root: Path) -> tuple[list[dict], Path]:
    """扫描全书，返回 (逐章指标, 章节目录)。"""
    ch_dir = next(
        (root / d for d in _CHAPTER_DIR_CANDIDATES if (root / d).exists()), None
    )
    if ch_dir is None:
        raise SystemExit(f"未找到章节目录（{'/'.join(_CHAPTER_DIR_CANDIDATES)}）：{root}")

    results: list[dict] = []
    for f in sorted(
        ch_dir.glob("Ch*_第*章.md"),
        key=lambda p: int(re.match(r"Ch(\d+)", p.name).group(1)),
    ):
        m = re.match(r"Ch(\d+)", f.name)
        ch = int(m.group(1)) if m else 0
        try:
            raw = f.read_text(encoding="utf-8")
            p = profile_one(raw)
            p["chapter"] = ch
            p["file"] = f.name
            p["quote_style"] = quote_style(raw)
            if not p.get("skip"):
                results.append(p)
        except Exception as exc:  # 单章失败不阻断全书
            print(f"  ⚠️ {f.name}: {exc}", file=sys.stderr)
    results.sort(key=lambda x: x["chapter"])
    return results, ch_dir


def _mean(xs: list[dict], key: str) -> float:
    return statistics.mean(x[key] for x in xs) if xs else 0.0


def _fmt_pct(x: float) -> str:
    return f"{x:.1%}"


def render_markdown(results: list[dict], bucket: int, scanned_at: str) -> str:
    """生成全量 Markdown 报告。"""
    n = len(results)
    L: list[str] = []
    L.append("# 全书文风扫描报告")
    L.append("")
    L.append(f"> 扫描时间：{scanned_at} ｜ 覆盖 {n} 章（正文原文直读，非记忆库）")
    L.append("")
    L.append("## 整体画像")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|------|------|")
    L.append(f"| 平均句长 | {_mean(results, 'avg_sent'):.1f} 字 |")
    L.append(f"| 平均段长 | {_mean(results, 'avg_para'):.0f} 字 |")
    L.append(f"| 对话行占比 | {_fmt_pct(_mean(results, 'dlg_line_ratio'))} |")
    L.append(f"| 对话字数占比 | {_fmt_pct(_mean(results, 'speech_ratio'))} |")
    L.append(f"| 修辞密度 | {_mean(results, 'rhet'):.5f} |")
    L.append(f"| 平均每章字数 | {_mean(results, 'chars'):.0f} |")
    L.append("")

    # 引号风格断层
    L.append("## 引号风格分布")
    L.append("")
    changes: list[tuple[int, str]] = []
    prev: str | None = None
    for r in results:
        if prev and r["quote_style"] != prev and r["quote_style"] != "none":
            changes.append((r["chapter"], r["quote_style"]))
        if r["quote_style"] != "none":
            prev = r["quote_style"]
    curly = sum(1 for r in results if r["quote_style"] == "curly")
    ascii_n = sum(1 for r in results if r["quote_style"] == "ascii")
    L.append(f"全角弯引号 {curly} 章 / ASCII 直引号 {ascii_n} 章")
    if changes:
        L.append("")
        L.append("**风格切换点**（一致性信号，供统稿参考）：")
        for ch, st in changes:
            label = "全角弯引号" if st == "curly" else "ASCII直引号"
            L.append(f"- Ch{ch} → {label}")
    L.append("")

    # 分卷趋势
    L.append("## 分卷趋势")
    L.append("")
    L.append("| 卷 | 句长 | 对话行 | 对话字 | 修辞密度 | 章数 |")
    L.append("|----|------|--------|--------|----------|------|")
    buckets: dict[int, list[dict]] = {}
    for r in results:
        b = (r["chapter"] - 1) // bucket * bucket + 1
        buckets.setdefault(b, []).append(r)
    for b in sorted(buckets):
        rs = buckets[b]
        L.append(
            f"| Ch{b}-{b + bucket - 1} | {_mean(rs, 'avg_sent'):.1f} | "
            f"{_fmt_pct(_mean(rs, 'dlg_line_ratio'))} | "
            f"{_fmt_pct(_mean(rs, 'speech_ratio'))} | {_mean(rs, 'rhet'):.5f} | {len(rs)} |"
        )
    L.append("")

    # 漂移检测
    L.append("## 文风漂移（前 1/3 vs 后 1/3）")
    L.append("")
    third = max(n // 3, 1)
    front, back = results[:third], results[-third:]
    L.append("| 指标 | 前 1/3 | 后 1/3 | 变化 |")
    L.append("|------|--------|--------|------|")
    for key, label in [
        ("avg_sent", "平均句长"),
        ("dlg_line_ratio", "对话行占比"),
        ("speech_ratio", "对话字数占比"),
        ("rhet", "修辞密度"),
    ]:
        fv, bv = _mean(front, key), _mean(back, key)
        delta = bv - fv
        drift = "⚠️ 漂移" if abs(delta) > max(abs(fv) * 0.25, 0.02) else "稳定"
        L.append(f"| {label} | {fv:.4f} | {bv:.4f} | {delta:+.4f} ({drift}) |")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="全书文风扫描（正文原文直读）")
    ap.add_argument("root", type=Path, help="小说项目根目录（含 废灵根/ 等章节目录）")
    ap.add_argument("--bucket", type=int, default=20, help="分卷粒度（默认 20 章）")
    ap.add_argument("--out", default="cortexa-out", help="报告输出目录（相对 root，默认 cortexa-out）")
    args = ap.parse_args()

    results, ch_dir = scan(args.root)
    if not results:
        print(f"无可用章节：{ch_dir}", file=sys.stderr)
        return 1

    scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = render_markdown(results, args.bucket, scanned_at)

    # stdout 摘要
    n = len(results)
    print(f"章节数: {n}  均值句长 {_mean(results, 'avg_sent'):.1f}  "
          f"对话行 {_fmt_pct(_mean(results, 'dlg_line_ratio'))}  "
          f"对话字 {_fmt_pct(_mean(results, 'speech_ratio'))}")
    third = max(n // 3, 1)
    f, b = results[:third], results[-third:]
    fs, bs = _mean(f, "avg_sent"), _mean(b, "avg_sent")
    print(f"文风漂移: 句长 {fs:.1f} → {bs:.1f} "
          f"({'⚠️' if abs(bs - fs) > 3 else 'OK'})  "
          f"对话字 {_fmt_pct(_mean(f, 'speech_ratio'))} → {_fmt_pct(_mean(b, 'speech_ratio'))}")

    # 报告归位（root/cortexa-out/）
    out_dir = args.root / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_file = out_dir / f"style_scan_{stamp}.md"
    out_file.write_text(md, encoding="utf-8")
    # 覆盖最新指针（供后续工具/人工取最新报告）
    (out_dir / "style_scan_latest.md").write_text(md, encoding="utf-8")

    # 明细 JSON（供写作框架程序化消费）
    detail = out_dir / f"style_scan_{stamp}.json"
    detail.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "style_scan_latest.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"报告: {out_file}")
    print(f"明细: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
