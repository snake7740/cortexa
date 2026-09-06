"""T2.1 judge v1 判答夹具（2026-09-06）。

官方六 pipeline(Open-Ended answer + ACCURACY judge)的最小镜像：build 提示词、
answer(需 OPENAI_API_KEY, 用户本机跑分)、judge 顶替判答。沙箱无 key→仅离线
校验提示词结构(--check-offline)。口径：本地代理结果，非官方成绩。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cortexa.config import Settings  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402

OPEN_ANSWER = """You are asked to answer a question based on your memories of a conversation.
<instructions>
1. Use only the provided memories. Prefer the memory that answers the question most directly.
3. The question may contain typos. Match it to the most relevant memory even if the wording differs.
6. Preserve specific names, titles, places, and labels from the memories.
8. If memories conflict, prefer the most recent supported memory.
10. Keep the final answer minimal. No preamble, no fluff.
</instructions>

<memories>
{memories}
</memories>

Question: {question}
"""

ACC_JUDGE = """Your task is to label an answer as CORRECT or WRONG given:
(1) a question, (2) a gold answer, (3) a generated answer.
Core principle: Inclusion + Non-contradiction. Be GENEROUS: if the generated answer
clearly includes the gold key content (or a clear paraphrase) and does not contradict it,
mark CORRECT. Mark WRONG otherwise.

Question: {question}
Gold: {gold}
Answer: {answer}
Reply CORRECT or WRONG only.
"""

NL = chr(10)


def build_open_prompt(memories: list[str], question: str) -> str:
    return OPEN_ANSWER.format(memories=NL.join(memories), question=question)


def build_judge_prompt(question: str, gold: str, answer: str) -> str:
    return ACC_JUDGE.format(question=question, gold=gold, answer=answer)


def answer(settings: Settings, memories: list[str], question: str) -> str | None:
    """gpt-4o-mini 顶替判答（需 key；无 key 返回 None）。"""
    llm = LLMClient(settings)
    return llm.chat("You answer concisely from the provided memories.",
                    build_open_prompt(memories, question), max_tokens=64)


def main() -> None:
    ap = argparse.ArgumentParser(description="judge v1 判答夹具")
    ap.add_argument("--check-offline", action="store_true",
                    help="无 key 环境：只校验提示词结构")
    args = ap.parse_args()
    memories = ["Alice moved to Sweden in 2019.", "Alice adopted a dog named Rob."]
    q = "Where does Alice live now?"
    if args.check_offline:
        p = build_open_prompt(memories, q)
        assert "Where does Alice live now?" in p and "Sweden" in p
        assert "CORRECT or WRONG" in build_judge_prompt(q, "Sweden", "Sweden")
        print("[judge_v1] offline prompt check OK（跑分需本机 OPENAI_API_KEY）")
        return
    print(answer(Settings(_env_file=None), memories, q))


if __name__ == "__main__":
    main()
