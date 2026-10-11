"""Content-free request and provider timing; no messages, titles, or user IDs."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger("bookclub.performance")


@dataclass
class RequestMetrics:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=perf_counter)
    llm_calls: int = 0
    llm_seconds: float = 0.0
    llm_failures: int = 0
    failed: bool = False


current_request: ContextVar[RequestMetrics | None] = ContextVar("request_metrics", default=None)


def start_request():
    return current_request.set(RequestMetrics())


def finish_request(token, action: str | None = None):
    metrics = current_request.get()
    try:
        logger.info(
            "request request_id=%s action=%s duration_ms=%.1f llm_calls=%d llm_ms=%.1f llm_failures=%d failed=%s",
            metrics.request_id, action or "none", (perf_counter() - metrics.started) * 1000,
            metrics.llm_calls, metrics.llm_seconds * 1000, metrics.llm_failures, metrics.failed,
        )
    finally:
        current_request.reset(token)


def mark_failure():
    metrics = current_request.get()
    if metrics is not None:
        metrics.failed = True


def record_llm(provider: str, seconds: float, failed: bool, input_tokens=0, output_tokens=0,
               cache_read_tokens=0, cache_write_tokens=0):
    metrics = current_request.get()
    if metrics is not None:
        metrics.llm_calls += 1
        metrics.llm_seconds += seconds
        metrics.llm_failures += int(failed)
    logger.info(
        "llm request_id=%s provider=%s duration_ms=%.1f failed=%s input_tokens=%d output_tokens=%d "
        "cache_read_tokens=%d cache_write_tokens=%d",
        metrics.request_id if metrics else "none", provider, seconds * 1000, failed,
        input_tokens or 0, output_tokens or 0, cache_read_tokens or 0, cache_write_tokens or 0,
    )
