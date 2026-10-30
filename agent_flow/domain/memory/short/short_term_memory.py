from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Literal


memory_field = Literal[
    "tool_respond",
    "agent_history",
    "error",
    "skill",
]


class ShortTermMemory(ABC):
    
    def __init__(self,fields:list[memory_field]):
        self.field = fields

    def iter_field(self, field: memory_field) -> list[tuple[str, str]]:
        """Compatibility fallback; implementations may preserve cross-key write order."""
        return [(key, raw) for key in self.keys_by_field(field)
                for index in range(1, self.count(field, key) + 1)
                if (raw := self.get(field, key, index)) is not None]

    def replace_prefix(self, field, expected, replacement) -> bool:
        """Replace a captured prefix, retaining writes appended during preparation."""
        current = self.iter_field(field)
        if current[:len(expected)] != list(expected):
            return False
        self.clear_field(field)
        for key, raw in [*replacement, *current[len(expected):]]:
            self.store(field, key, raw)
        return True


    @abstractmethod
    async def store(self,fields:memory_field, key: str, raw: str) -> int:
        """存入工具原始输出，返回第几次调用（从 1 开始）。"""
        ...

    @abstractmethod
    def get(self,fields:memory_field, key: str, index: int) -> str | None:
        """取第 index 次原文，index=0 表示最后一次。"""
        ...


    @abstractmethod
    def all_keys(self) -> list[tuple[str, str]]: ...

    @abstractmethod
    def keys_by_field(self, field: memory_field) -> list[str]: ...

    @abstractmethod
    def count(self, field:memory_field,key: str) -> int: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def clear_field(self, field: memory_field) -> None: ...

    @abstractmethod
    def delete_key(self, field: memory_field, key: str) -> None:
        """删除某 field 下指定 key 的全部条目（用于一次性消费）。"""
        ...
