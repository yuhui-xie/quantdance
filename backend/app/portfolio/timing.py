"""组合回测分阶段计时（用于定位耗时热点）。"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import TextIO, Iterable


@dataclass
class StageTimer:
    title: str = "portfolio"
    _t0: float = field(default_factory=time.perf_counter, init=False, repr=False)
    _last: float = field(default_factory=time.perf_counter, init=False, repr=False)
    stages: list[tuple[str, float]] = field(default_factory=list)
    extras: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def mark(self, name: str) -> float:
        now = time.perf_counter()
        dt = now - self._last
        self.stages.append((name, dt))
        self._last = now
        return dt

    def add(self, name: str, seconds: float) -> None:
        self.extras[name] = self.extras.get(name, 0.0) + float(seconds)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def total(self) -> float:
        return time.perf_counter() - self._t0

    def format(self) -> str:
        rows = list(self.stages)
        total = self.total()
        width = max((len(n) for n, _ in rows), default=8)
        for k in self.extras:
            width = max(width, len(k))
        lines = [f"[{self.title} timing] total={total:.2f}s"]
        for name, dt in rows:
            pct = (100.0 * dt / total) if total > 0 else 0.0
            lines.append(f"  {name:<{width}}  {dt:8.2f}s  {pct:5.1f}%")
        if self.extras:
            lines.append("  -- detail --")
            for name, dt in sorted(self.extras.items(), key=lambda x: -x[1]):
                pct = (100.0 * dt / total) if total > 0 else 0.0
                lines.append(f"  {name:<{width}}  {dt:8.2f}s  {pct:5.1f}%")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)

    def print(self, file: TextIO | None = None) -> None:
        print(self.format(), file=file or sys.stderr, flush=True)


def merge_timers(timers: Iterable[StageTimer], *, title: str) -> StageTimer:
    out = StageTimer(title=title)
    for t in timers:
        for name, dt in t.stages:
            out.stages.append((name, dt))
        for k, v in t.extras.items():
            out.add(k, v)
        out.notes.extend(t.notes)
    out._t0 = time.perf_counter() - sum(dt for _, dt in out.stages)
    out._last = time.perf_counter()
    return out
