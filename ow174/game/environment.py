"""Clock and secure-entropy boundaries of the game server and its matches.

The default delegates each read to its original system source. Explicitly injected
environments can check the ordered reads without starting a server. This boundary
does not record network events, collision queries or complete match state.
"""

import secrets
import time
from typing import Protocol


class ExecutionEnvironment(Protocol):
    """External reads; callsite identifies each read without adding another one."""

    def wall_time(self, *, callsite: str) -> float: ...

    def wall_time_ns(self, *, callsite: str) -> int: ...

    def monotonic(self, *, callsite: str) -> float: ...

    def randbits(self, bits: int, *, callsite: str) -> int: ...

    def randbelow(self, upper: int, *, callsite: str) -> int: ...

    def token_bytes(self, length: int, *, callsite: str) -> bytes: ...


class SystemEnvironment:
    """Real clocks and secrets-backed entropy, with no caching or seeded mode."""

    def wall_time(self, *, callsite: str) -> float:
        return time.time()

    def wall_time_ns(self, *, callsite: str) -> int:
        return time.time_ns()

    def monotonic(self, *, callsite: str) -> float:
        return time.monotonic()

    def randbits(self, bits: int, *, callsite: str) -> int:
        return secrets.randbits(bits)

    def randbelow(self, upper: int, *, callsite: str) -> int:
        return secrets.randbelow(upper)

    def token_bytes(self, length: int, *, callsite: str) -> bytes:
        return secrets.token_bytes(length)
