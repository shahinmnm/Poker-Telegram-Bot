"""Type hints for redis client dependencies used across the poker application."""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Protocol, Sequence


class RedisClient(Protocol):
    """Protocol describing redis operations required by the 2PC components."""

    async def eval(
        self, script: str, keys: Sequence[str], args: Sequence[Any]
    ) -> Any:
        ...

    async def hgetall(self, key: str) -> Mapping[str, Any]:
        ...

    async def expire(self, key: str, ttl: int) -> bool:
        ...

    async def delete(self, key: str) -> int:
        ...

    async def lpush(self, key: str, value: Any) -> int:
        ...

    async def exists(self, key: str) -> bool:
        ...

    async def hset(self, key: str, mapping: MutableMapping[str, Any]) -> int:
        ...

