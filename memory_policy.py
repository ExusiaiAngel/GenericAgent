"""Deterministic size and retention policy for GenericAgent memory."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class MemoryBudget:
    max_bytes: int
    max_lines: int | None = None


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(1, value)


def budget_for(path: str | os.PathLike) -> MemoryBudget:
    name = Path(path).name
    if name == "global_mem_insight.txt":
        return MemoryBudget(
            _positive_env("GENERICAGENT_MEMORY_L1_MAX_BYTES", 4096),
            _positive_env("GENERICAGENT_MEMORY_L1_MAX_LINES", 30),
        )
    if name == "global_mem.txt":
        return MemoryBudget(_positive_env("GENERICAGENT_MEMORY_L2_MAX_BYTES", 16384))
    if name == "personal_bootstrap_profile.md":
        return MemoryBudget(_positive_env("GENERICAGENT_MEMORY_PROFILE_MAX_BYTES", 8192))
    return MemoryBudget(_positive_env("GENERICAGENT_MEMORY_L3_MAX_BYTES", 16384))


def validate_memory_content(
    path: str | os.PathLike,
    content: str,
    *,
    previous_content: str = "",
    automatic: bool = False,
) -> None:
    encoded = str(content).encode("utf-8")
    budget = budget_for(path)
    if len(encoded) > budget.max_bytes:
        raise ValueError(
            f"memory budget exceeded for {Path(path).name}: "
            f"{len(encoded)} bytes > {budget.max_bytes}; consolidate before writing"
        )
    if budget.max_lines is not None:
        lines = len(str(content).splitlines())
        if lines > budget.max_lines:
            raise ValueError(
                f"memory line budget exceeded for {Path(path).name}: "
                f"{lines} lines > {budget.max_lines}; consolidate before writing"
            )
    if automatic:
        growth = len(encoded) - len(str(previous_content).encode("utf-8"))
        max_growth = _positive_env("GENERICAGENT_MEMORY_SETTLEMENT_MAX_GROWTH", 1024)
        if growth > max_growth:
            raise ValueError(
                f"automatic memory growth exceeded: {growth} bytes > {max_growth}"
            )


def validate_injected_memory(path: str | os.PathLike, content: str) -> str:
    validate_memory_content(path, content)
    return content


def plan_l4_retention(
    files: Iterable[Path], *, now: float, max_age_days: int, max_total_bytes: int
) -> list[Path]:
    """Return regular files eligible for deletion; never mutates the filesystem."""
    regular = [p for p in files if p.is_file() and not p.is_symlink()]
    remove = {
        p for p in regular
        if now - p.stat().st_mtime > max(0, max_age_days) * 86400
    }
    remaining = sorted(
        (p for p in regular if p not in remove),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    total = sum(p.stat().st_size for p in remaining)
    for path in remaining:
        if total <= max_total_bytes:
            break
        remove.add(path)
        total -= path.stat().st_size
    return sorted(remove, key=lambda p: (p.stat().st_mtime, p.name))
