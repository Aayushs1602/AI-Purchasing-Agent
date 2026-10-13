"""Running agent work off the request thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Executor(Protocol):
    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any: ...


class InlineExecutor:
    """Runs work immediately on the calling thread (tests, scripts)."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)


def make_executor(workers: int) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-run")


def run_in_background(
    executor: Executor, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> None:
    """Submit work and make sure a crash is logged (thread pools swallow exceptions)."""

    def logged() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Background task %s failed", getattr(fn, "__name__", fn))

    executor.submit(logged)
