"""CastRegistry（L2 领域模型）：角色/实体注册 + 别名消解。

设计文档：docs/design/scene-aware-architecture.md（v2.1 §6 Cast Registry）

解决的问题（图分析实证）："长生"与"叶长生"、"铁柱"与"赵铁柱"重复计数；
统计与检索时别名不归一导致分裂。

用法：
  registry = CastRegistry(get_schema("novel"))
  registry.canonicalize("长生")  # -> "叶长生"
  registry.canonical("叶长生")    # -> "叶长生"
"""

from __future__ import annotations

from ..schema import SceneSchema, get_schema


class CastRegistry:
    """schema.aliases 驱动的角色注册/别名消解（纯逻辑，不碰存储）。"""

    def __init__(self, schema: SceneSchema | None = None) -> None:
        self._schema = schema or get_schema("novel")

    @property
    def schema(self) -> SceneSchema:
        return self._schema

    def canonicalize(self, name: str) -> str:
        """别名 → 主名；非别名原样返回。"""
        return self._schema.aliases.get(name, name)

    def is_alias(self, name: str) -> bool:
        return name in self._schema.aliases

    def canonical_names(self) -> set[str]:
        """全部主名（characters + aliases 值去重）。"""
        names = set(self._schema.characters)
        names.update(self._schema.aliases.values())
        return names

    def known_names(self) -> list[str]:
        """全部主名 + 别名（供文本匹配；长名在前防前缀误匹配）。"""
        names = set(self._schema.characters)
        names.update(self._schema.aliases.keys())
        names.update(self._schema.aliases.values())
        return sorted(names, key=len, reverse=True)

    def all_names(self) -> list[str]:
        """同 known_names（兼容旧名）。"""
        return self.known_names()
