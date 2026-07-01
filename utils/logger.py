
import asyncio
import contextvars
import functools
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from utils.env import discover_workspace_root


_LOGGER_CACHE: dict[str, logging.Logger] = {}
_REDACTED_KEYS = {"authorization", "password", "secret", "token", "pat", "api_key"}
_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("audit_request_id", default=None)
_REQUEST_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("audit_request_depth", default=0)


def _audit_log_path() -> Path:
    root_dir = discover_workspace_root(Path(__file__))
    audit_dir = root_dir / "AI_Tracking" / "Audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / "mcp-audit.log"


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    cached = _LOGGER_CACHE.get(name)
    if cached:
        return cached

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        file_handler = logging.FileHandler(_audit_log_path())
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _LOGGER_CACHE[name] = logger
    return logger


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lower_key = str(key).lower()
            if any(fragment in lower_key for fragment in _REDACTED_KEYS):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = _sanitize_value(item)
        return sanitized

    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]

    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(value)
                except Exception:
                    parsed = None
                if parsed is not None:
                    return _summarize_structured_payload(parsed)

            text = value[:500] + "..." if len(value) > 500 else value
            return text

        text = value
        return text

    return repr(value)


def _summarize_structured_payload(value: Any) -> Any:
    if isinstance(value, dict):
        summary: dict[str, Any] = {"_type": "dict", "keys": sorted(value.keys())[:20]}
        if "counts" in value and isinstance(value["counts"], dict):
            summary["counts"] = _sanitize_value(value["counts"])
        if "user_email" in value:
            summary["user_email"] = value["user_email"]
        if "project" in value:
            summary["project"] = value["project"]
        if "remaining_story_points" in value:
            summary["remaining_story_points"] = value["remaining_story_points"]
        if "items" in value and isinstance(value["items"], list):
            summary["item_count"] = len(value["items"])
            summary["item_ids"] = [item.get("id") for item in value["items"][:10] if isinstance(item, dict)]
        if "blocked_items" in value and isinstance(value["blocked_items"], list):
            summary["blocked_count"] = len(value["blocked_items"])
        if "open_prs" in value and isinstance(value["open_prs"], list):
            summary["open_pr_count"] = len(value["open_prs"])
        return summary

    if isinstance(value, list):
        summary = {"_type": "list", "length": len(value)}
        if value and all(isinstance(item, dict) for item in value[:10]):
            summary["ids"] = [item.get("id") for item in value[:10]]
        return summary

    return value


def _format_payload(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    payload = {
        "args": _sanitize_value(args),
        "kwargs": _sanitize_value(kwargs),
    }
    return json.dumps(payload, ensure_ascii=True, default=str)


class AuditLogger:
    def __init__(self, logger_name: str):
        self.logger = get_logger(logger_name)

    def _enter_context(self) -> tuple[str, int, bool, contextvars.Token[int], contextvars.Token[str | None] | None]:
        request_id = _REQUEST_ID.get()
        created_request_id = False
        request_token: contextvars.Token[str | None] | None = None
        if request_id is None:
            request_id = uuid.uuid4().hex[:12]
            request_token = _REQUEST_ID.set(request_id)
            created_request_id = True

        depth = _REQUEST_DEPTH.get()
        depth_token = _REQUEST_DEPTH.set(depth + 1)
        return request_id, depth, created_request_id, depth_token, request_token

    def _exit_context(
        self,
        depth_token: contextvars.Token[int],
        request_token: contextvars.Token[str | None] | None,
        created_request_id: bool,
    ) -> None:
        _REQUEST_DEPTH.reset(depth_token)
        if created_request_id and request_token is not None:
            _REQUEST_ID.reset(request_token)

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                request_id, depth, created_request_id, depth_token, request_token = self._enter_context()
                started_at = time.perf_counter()
                self.logger.info(
                    "START request_id=%s depth=%s fn=%s payload=%s",
                    request_id,
                    depth,
                    func.__name__,
                    _format_payload(args, kwargs),
                )
                try:
                    result = await func(*args, **kwargs)
                except Exception:
                    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                    self.logger.exception(
                        "ERROR request_id=%s depth=%s fn=%s duration_ms=%s",
                        request_id,
                        depth,
                        func.__name__,
                        duration_ms,
                    )
                    self._exit_context(depth_token, request_token, created_request_id)
                    raise

                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                self.logger.info(
                    "END request_id=%s depth=%s fn=%s duration_ms=%s",
                    request_id,
                    depth,
                    func.__name__,
                    duration_ms,
                )
                self._exit_context(depth_token, request_token, created_request_id)
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            request_id, depth, created_request_id, depth_token, request_token = self._enter_context()
            started_at = time.perf_counter()
            self.logger.info(
                "START request_id=%s depth=%s fn=%s payload=%s",
                request_id,
                depth,
                func.__name__,
                _format_payload(args, kwargs),
            )
            try:
                result = func(*args, **kwargs)
            except Exception:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                self.logger.exception(
                    "ERROR request_id=%s depth=%s fn=%s duration_ms=%s",
                    request_id,
                    depth,
                    func.__name__,
                    duration_ms,
                )
                self._exit_context(depth_token, request_token, created_request_id)
                raise

            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            self.logger.info(
                "END request_id=%s depth=%s fn=%s duration_ms=%s",
                request_id,
                depth,
                func.__name__,
                duration_ms,
            )
            self._exit_context(depth_token, request_token, created_request_id)
            return result

        return sync_wrapper


def audit(logger_name: str) -> AuditLogger:
    return AuditLogger(logger_name)