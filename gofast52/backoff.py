"""Exponential backoff with full jitter and Retry-After support (SPEC 4.3).

Defaults follow the researched consensus: base 1s, factor 2, cap 60s,
AWS-style full jitter, Retry-After + uniform(0,1) jitter capped at 1800s.
"""
from __future__ import annotations

import random
from typing import Optional, Protocol


class Rng(Protocol):
    def random(self) -> float: ...


class BackoffPolicy:
    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        cap: float = 60.0,
        jitter: str = "full",
        rng: Optional[Rng] = None,
        retry_after_cap: float = 1800.0,
    ):
        if base <= 0 or factor <= 1 or cap <= 0:
            raise ValueError("invalid backoff parameters")
        if jitter not in ("full", "none"):
            raise ValueError(f"unknown jitter mode: {jitter}")
        self.base = base
        self.factor = factor
        self.cap = cap
        self.jitter = jitter
        self.rng = rng if rng is not None else random
        self.retry_after_cap = retry_after_cap

    def upper_bound(self, attempt: int) -> float:
        return min(self.cap, self.base * (self.factor ** max(0, attempt)))

    def delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        ub = self.upper_bound(attempt)
        if retry_after is not None and retry_after > 0:
            ra = min(max(retry_after, 0.0), self.retry_after_cap)
            return min(max(ub, ra + self.rng.random() * 1.0), max(self.retry_after_cap, ub))
        if self.jitter == "full":
            return self.rng.random() * ub
        return ub
