"""Shared types + concurrency primitives for Agent 3 checks."""
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import enum
import logging
import time
from typing import Any, Callable, Iterable

log = logging.getLogger("agent3")


class Severity(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclasses.dataclass
class CheckResult:
    name: str
    severity: Severity
    summary: str
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
    elapsed_ms: int = 0
    target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["severity"] = self.severity.value
        return d


def timed(fn: Callable[..., CheckResult]) -> Callable[..., CheckResult]:
    """Decorator: measure elapsed_ms on a CheckResult."""
    def wrap(*args, **kwargs) -> CheckResult:
        t0 = time.monotonic()
        try:
            res = fn(*args, **kwargs)
        except Exception as e:  # never let a check crash the orchestrator
            res = CheckResult(
                name=getattr(fn, "__check_name__", fn.__name__),
                severity=Severity.FAIL,
                summary=f"unhandled exception: {e!r}",
                details={"exception": repr(e)},
            )
        res.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return res
    wrap.__name__ = fn.__name__
    return wrap


def run_checks_parallel(
    tasks: Iterable[tuple[Callable[..., CheckResult], tuple, dict]],
    max_workers: int = 16,
) -> list[CheckResult]:
    """Execute (fn, args, kwargs) tuples concurrently and return results in submission order."""
    results: list[CheckResult] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(fn, *a, **kw) for fn, a, kw in tasks]
        for fut in futures:
            results.append(fut.result())
    return results
