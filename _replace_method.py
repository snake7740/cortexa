import io
p = r"C:\Cortexa\cortexa-mvp\cortexa\modules\novel_adapter.py"
src = open(p, encoding="utf-8").read()

# 定位方法体起止（用行号锚点）
lines = src.split("\n")
# 找 def infer_relations 和下一个 P1 注释
def_idx = next(i for i, l in enumerate(lines) if "def infer_relations" in l)
p1_idx = next(i for i, l in enumerate(lines) if "P1: LifecycleGuard" in l)

new_method = """    def infer_relations(
        self,
        text: str,
        known_entities: list[str],
        chapter: int | None,
        scope: str,
    ) -> list[dict]:
        """从原文提取显式关系，生成建边请求（graph.add_link 参数）。

        防幻觉约束：
        - 只处理显式关系表述（拜师/结为道侣/父子 等），不从同框出现推断；
        - 关系双方必须都在 known_entities（已注册实体）中，否则跳过；
        - 每条携带 evidence（原文片段），供 graph.add_link 留痕。

        Returns: [{src, dst, link_type, evidence, evidence_chapter}, ...]
        """
        if chapter is None or not known_entities:
            return []
        found: list[dict] = []
        # 按句子处理
        for sent in re.split(r"[。！？!?\n]+", text):
            for pattern, link_type in self._RELATION_PATTERNS:
                m = re.search(pattern, sent)
                if not m:
                    continue
                # 句中出现的已知实体（去重保序）
                present: list[str] = []
                for e in known_entities:
                    if e in sent and e not in present:
                        present.append(e)
                if len(present) < 2:
                    continue
                # 关系词命中区间中点作关系轴
                axis = (m.start() + m.end()) / 2
                left: str | None = None
                right: str | None = None
                left_pos = -1
                right_pos = 10**9
                for e in present:
                    pos = sent.find(e)
                    if pos < axis and pos > left_pos:
                        left = e
                        left_pos = pos
                    elif pos > axis and pos < right_pos:
                        right = e
                        right_pos = pos
                if left and right:
                    found.append({
                        "src": left,
                        "dst": right,
                        "link_type": link_type,
                        "evidence": sent.strip()[:120],
                        "evidence_chapter": chapter,
                    })
                break  # 一句只取第一个命中关系
        # 去重（同 src/dst/type）
        uniq: list[dict] = []
        seen_k = set()
        for r in found:
            key = (r["src"], r["dst"], r["link_type"])
            if key not in seen_k:
                seen_k.add(key)
                uniq.append(r)
        return uniq

"""
# 替换（保留 P1 注释前的缩进结构）
lines[def_idx:p1_idx] = [""]  # 先清掉旧方法
# 在 P1 注释前插入新方法（保持类缩进4空格）
new_lines = [("    " + l if l.strip() else l) for l in new_method.split("\n")]
# 去掉首尾空串
new_lines = [l for l in new_lines if l != "    "]
# 重建：清空区替换为 new_lines（每行前面加4空格已在上面做）
lines[def_idx:p1_idx] = new_lines
open(p, "w", encoding="utf-8").write("\n".join(lines))
print("REPLACED")