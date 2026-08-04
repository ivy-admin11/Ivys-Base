"""Detached worker that owns an Ivy job's observable lifecycle.

``job_runner`` launches this module in a new session and passes the canonical
``module:function`` entrypoint plus an execution ID.  The worker, rather than
the short-lived dispatcher, records the eventual result.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import JOB_HEARTBEAT_SECONDS, JOB_MAX_RUNTIME_SECONDS
from ivy_core import receipts

logger = logging.getLogger("ivy.job_worker")

_FAILURE_OUTCOMES = frozenset({
    "error",
    "failed",
    "failure",
    "auth_failure",
    "upstream_unavailable",
    "internal_error",
})
_SKIP_OUTCOMES = frozenset({"skipped", "already_running"})
_NO_DELIVERY_OUTCOMES = frozenset({
    "duplicate",
    "no_picks",
    "no_qualifying_picks",
    "skipped",
    "already_running",
})

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)\s*[=:]\s*)[^&\s]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_QUERY = re.compile(r"(https?://[^?\s]+)\?[^\s]+")
_SENSITIVE_KEY_PARTS = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)
_RECEIPT_RESULT_FIELDS = frozenset({
    "alert_sent",
    "attached",
    "consensus_count",
    "delivery_status",
    "deliveries",
    "discovery_count",
    "error_type",
    "lifecycle_status",
    "picks_count",
    "recipe_count",
    "report_id",
    "report_ids",
    "result_type",
    "sent",
    "status",
})
_RECEIPT_DELIVERY_FIELDS = frozenset({
    "attachment_status",
    "channel",
    "error_category",
    "fallback_messages_attempted",
    "fallback_messages_submitted",
    "fallback_status",
    "notice_status",
    "notification_status",
    "purpose",
    "report_id",
    "status",
})


def _safe_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value)
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _BEARER_TOKEN.sub("Bearer [redacted]", text)
    text = _URL_QUERY.sub(r"\1?[redacted]", text)
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text[:limit]


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_result(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, JSON-safe result with common secret forms redacted."""
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__}
    if isinstance(value, Mapping):
        cleaned: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                cleaned["_truncated"] = True
                break
            key_text = _safe_text(key, limit=120)
            if _is_sensitive_key(key_text):
                cleaned[key_text] = "[redacted]"
            else:
                cleaned[key_text] = sanitize_result(item, depth=depth + 1)
        return cleaned
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            sanitize_result(item, depth=depth + 1)
            for item in islice(value, 100)
        ]
    return _safe_text(value)


def summarize_result_for_receipt(value: Any) -> Any:
    """Persist only lifecycle/delivery metadata, never arbitrary agent output."""
    if not isinstance(value, Mapping):
        return {"value_type": type(value).__name__}
    summary: Dict[str, Any] = {}
    for key in _RECEIPT_RESULT_FIELDS:
        if key not in value:
            continue
        if key == "deliveries":
            deliveries = value.get(key)
            if isinstance(deliveries, Sequence) and not isinstance(
                deliveries, (str, bytes, bytearray)
            ):
                summary[key] = [
                    {
                        field: sanitize_result(item[field])
                        for field in _RECEIPT_DELIVERY_FIELDS
                        if field in item
                    }
                    for item in islice(deliveries, 100)
                    if isinstance(item, Mapping)
                ]
            continue
        summary[key] = sanitize_result(value[key])
    return summary


def _normalize_status_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    return text or None


def _delivery_value(value: Any) -> Optional[str]:
    if value is True:
        return "submitted_unverified"
    if value is False:
        return "failed"
    normalized = _normalize_status_value(value)
    if normalized in receipts.DELIVERY_STATUSES:
        return normalized
    return None


def _aggregate_delivery(values: List[str]) -> Optional[str]:
    values = [value for value in values if value in receipts.DELIVERY_STATUSES]
    if not values:
        return None
    positive = {"submitted_unverified", "verified_delivered"}
    has_positive = any(value in positive for value in values)
    has_failure = any(value in {"failed", "unknown"} for value in values)
    if has_positive and has_failure:
        return "partial"
    if all(value == "verified_delivered" for value in values):
        return "verified_delivered"
    if has_positive:
        return "submitted_unverified"
    if all(value == "not_requested" for value in values):
        return "not_requested"
    if all(value == "not_attempted" for value in values):
        return "not_attempted"
    if any(value == "partial" for value in values):
        return "partial"
    if all(value == "failed" for value in values):
        return "failed"
    return "unknown"


def _extract_report_ids(result: Mapping[str, Any]) -> List[str]:
    found: List[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        report_id = _safe_text(value, limit=200).strip()
        if report_id and report_id not in found and len(found) < 100:
            found.append(report_id)

    add(result.get("report_id"))
    report_ids = result.get("report_ids")
    if isinstance(report_ids, Sequence) and not isinstance(report_ids, (str, bytes, bytearray)):
        for report_id in islice(report_ids, 100):
            add(report_id)
    deliveries = result.get("deliveries")
    if isinstance(deliveries, Sequence) and not isinstance(deliveries, (str, bytes, bytearray)):
        for delivery in islice(deliveries, 100):
            if isinstance(delivery, Mapping):
                add(delivery.get("report_id"))
    return found


def normalize_result(
    result: Any,
    *,
    send: bool,
) -> Tuple[str, str, str, List[str], Any]:
    """Map heterogeneous agent results to lifecycle/outcome/delivery fields."""
    safe_result = summarize_result_for_receipt(result)
    if not isinstance(result, Mapping):
        delivery = "not_requested" if not send else "unknown"
        return "completed", "unstructured_result", delivery, [], safe_result

    raw_status = _normalize_status_value(result.get("status"))
    result_type = _normalize_status_value(result.get("result_type"))
    explicit_lifecycle = _normalize_status_value(result.get("lifecycle_status"))

    if explicit_lifecycle in receipts.TERMINAL_STATUSES:
        lifecycle = explicit_lifecycle
    elif raw_status in _SKIP_OUTCOMES or result_type in _SKIP_OUTCOMES:
        lifecycle = "skipped"
    elif raw_status in _FAILURE_OUTCOMES or result_type in _FAILURE_OUTCOMES:
        lifecycle = "failed"
    else:
        lifecycle = "completed"

    if raw_status == "success" and result_type and result_type != "picks":
        outcome = result_type
    else:
        outcome = raw_status or result_type or "unspecified"

    report_ids = _extract_report_ids(result)
    explicit_delivery = _delivery_value(result.get("delivery_status"))
    if not send:
        delivery = "not_requested"
    elif explicit_delivery:
        delivery = explicit_delivery
    else:
        attachment_values: List[str] = []
        attachment_status = result.get("attachment_status")
        if isinstance(attachment_status, Mapping):
            attachment_values.extend(
                status for status in (_delivery_value(value) for value in attachment_status.values()) if status
            )
        elif attachment_status is not None:
            status = _delivery_value(attachment_status)
            if status:
                attachment_values.append(status)

        delivery_values: List[str] = []
        deliveries = result.get("deliveries")
        if isinstance(deliveries, Sequence) and not isinstance(deliveries, (str, bytes, bytearray)):
            for item in deliveries:
                if isinstance(item, Mapping):
                    status = _delivery_value(item.get("status"))
                    if status:
                        delivery_values.append(status)

        if attachment_values:
            delivery_values.extend(attachment_values)
            recipient_status = result.get("recipients_status")
            if all(value == "failed" for value in attachment_values) and isinstance(recipient_status, Mapping):
                # A successful value here may only be the failure notice, not
                # the report body.  Record partial rather than delivered.
                if any(value is True for value in recipient_status.values()):
                    delivery_values.append("submitted_unverified")
        else:
            for key in ("sent", "alert_sent"):
                if key in result:
                    value = result.get(key)
                    # A bare False says only that delivery did not happen; it
                    # does not distinguish an attempted send from an agent that
                    # intentionally had nothing to send.  A report ID is the
                    # minimum corroborating evidence for calling it failed.
                    status = (
                        "failed"
                        if value is False and report_ids
                        else "unknown" if value is False
                        else _delivery_value(value)
                    )
                    if status:
                        delivery_values.append(status)

        aggregated = _aggregate_delivery(delivery_values)
        if lifecycle == "skipped" or outcome in _NO_DELIVERY_OUTCOMES:
            delivery = (
                aggregated
                if aggregated in {"submitted_unverified", "verified_delivered", "partial"}
                else "not_attempted"
            )
        elif lifecycle == "failed" and not report_ids and not attachment_values:
            delivery = "not_attempted"
        elif aggregated:
            delivery = aggregated
        elif report_ids:
            delivery = "unknown"
        else:
            delivery = "not_attempted"

    return lifecycle, outcome, delivery, report_ids, safe_result


def _resolve_entrypoint(entrypoint: str) -> Callable[..., Any]:
    if ":" not in entrypoint:
        raise ValueError("Entrypoint must use module:function syntax")
    module_name, function_name = entrypoint.split(":", 1)
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError("Entrypoint target is not callable")
    return function


def _heartbeat_loop(execution_id: str, stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        try:
            if not receipts.record_heartbeat(execution_id):
                return
        except Exception:
            logger.warning("Could not update heartbeat for execution %s", execution_id)


def _timeout_watchdog(
    execution_id: str,
    stop: threading.Event,
    timeout_seconds: float,
    send: bool,
    hard_exit: Callable[[int], None],
) -> None:
    """Make a hung worker terminal, then end the process with exit code 124.

    Python cannot safely stop an arbitrary function running in another thread.
    The worker is already a dedicated child process, so process termination is
    the reliable isolation boundary.  The receipt CAS ensures a result that
    completed at the deadline wins instead of being overwritten by timeout.
    """
    if stop.wait(timeout_seconds):
        return
    should_exit = True
    try:
        should_exit = receipts.record_finish(
            execution_id,
            "timed_out",
            f"Job exceeded its {int(timeout_seconds)}-second runtime limit.",
            outcome="runtime_timeout",
            exit_code=124,
            result={
                "error_type": "JobTimeout",
                "max_runtime_seconds": int(timeout_seconds),
            },
            delivery_status="unknown" if send else "not_requested",
            report_ids=[],
        )
    except Exception as exc:
        # Enforce the process deadline even if the durable store is temporarily
        # unavailable.  Reconciliation will later mark the dead lease unknown.
        logger.error(
            "Could not record timeout for execution %s (%s)",
            execution_id,
            type(exc).__name__,
        )
    if should_exit:
        hard_exit(124)


def _terminal_exit_code(record: Mapping[str, Any]) -> int:
    stored = record.get("exit_code")
    if isinstance(stored, int):
        return stored
    return 0 if record.get("status") in {"completed", "skipped"} else 1


def run_execution(
    execution_id: str,
    *,
    entrypoint: str,
    force: bool,
    send: bool,
    heartbeat_interval: float = 15.0,
    max_runtime_seconds: float = float(JOB_MAX_RUNTIME_SECONDS),
) -> Tuple[int, Any]:
    """Execute one registered callable and finalize its receipt."""
    try:
        record = receipts.get_execution(execution_id, reconcile=False)
    except Exception as exc:
        logger.error(
            "Could not load execution %s (%s)", execution_id, type(exc).__name__
        )
        return 2, None
    if not record:
        logger.error("Execution receipt does not exist: %s", execution_id)
        return 2, None
    if record.get("status") in receipts.TERMINAL_STATUSES:
        return _terminal_exit_code(record), record.get("result")

    try:
        claimed = receipts.record_running(execution_id, pid=os.getpid())
    except Exception as exc:
        logger.error(
            "Could not claim execution %s (%s)", execution_id, type(exc).__name__
        )
        return 2, None
    if not claimed:
        try:
            current = receipts.get_execution(execution_id, reconcile=False)
        except Exception as exc:
            logger.error(
                "Could not reload unclaimed execution %s (%s)",
                execution_id,
                type(exc).__name__,
            )
            return 2, None
        if current and current.get("status") in receipts.TERMINAL_STATUSES:
            return _terminal_exit_code(current), current.get("result")
        logger.error("Execution %s is owned by another worker", execution_id)
        return 2, None

    requester = record.get("requester")
    stop = threading.Event()
    heartbeat_thread: Optional[threading.Thread] = None
    timeout_thread: Optional[threading.Thread] = None
    if heartbeat_interval > 0:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(execution_id, stop, heartbeat_interval),
            name=f"ivy-job-heartbeat-{execution_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
    if max_runtime_seconds > 0:
        timeout_thread = threading.Thread(
            target=_timeout_watchdog,
            args=(
                execution_id,
                stop,
                max_runtime_seconds,
                send,
                os._exit,
            ),
            name=f"ivy-job-timeout-{execution_id[:8]}",
            daemon=True,
        )
        timeout_thread.start()

    try:
        function = _resolve_entrypoint(entrypoint)
        result = function(
            force=force,
            send=send,
            requester=requester,
            request_id=execution_id,
        )
        lifecycle, outcome, delivery, report_ids, safe_result = normalize_result(
            result, send=send
        )
        exit_code = 0 if lifecycle in {"completed", "skipped"} else 1
        try:
            finalized = receipts.record_finish(
                execution_id,
                lifecycle,
                f"Agent returned outcome '{_safe_text(outcome, limit=120)}'.",
                outcome=outcome,
                exit_code=exit_code,
                result=safe_result,
                delivery_status=delivery,
                report_ids=report_ids,
            )
        except Exception as exc:
            logger.error(
                "Could not finalize execution %s (%s)",
                execution_id,
                type(exc).__name__,
            )
            return 2, safe_result
        if not finalized:
            logger.warning("Execution %s was already terminal", execution_id)
            current = receipts.get_execution(execution_id, reconcile=False) or {}
            return _terminal_exit_code(current), current.get("result")
        return exit_code, safe_result
    except BaseException as exc:  # includes SystemExit/KeyboardInterrupt from agent code
        error_type = type(exc).__name__
        logger.error("Job execution %s failed with %s", execution_id, error_type)
        try:
            receipts.record_finish(
                execution_id,
                "failed",
                f"{error_type}: job execution failed.",
                outcome=f"exception:{error_type}",
                exit_code=1,
                result={"error_type": error_type},
                delivery_status="unknown" if send else "not_requested",
                report_ids=[],
            )
        except Exception as receipt_exc:
            logger.error(
                "Could not record failure for execution %s (%s)",
                execution_id,
                type(receipt_exc).__name__,
            )
        return 1, {"error_type": error_type}
    finally:
        stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        if timeout_thread is not None:
            timeout_thread.join(timeout=1)


def _heartbeat_interval_from_env() -> float:
    return float(JOB_HEARTBEAT_SECONDS)


def _bounded_runtime_arg(value: str) -> float:
    try:
        runtime = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be numeric") from exc
    if not 30 <= runtime <= 21600:
        raise argparse.ArgumentTypeError("timeout must be between 30 and 21600 seconds")
    return runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ivy detached job worker")
    parser.add_argument("--execution-id")
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--requester")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument(
        "--timeout-seconds",
        type=_bounded_runtime_arg,
        default=float(JOB_MAX_RUNTIME_SECONDS),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    execution_id = args.execution_id
    if not execution_id:
        try:
            execution_id = receipts.record_start(
                args.job_name,
                requester=args.requester or "scheduled",
                executor="entrypoint",
                delivery_status="not_attempted" if args.send else "not_requested",
            )
        except receipts.ExecutionAlreadyActive as active:
            print(json.dumps({
                "status": "already_running",
                "execution_id": active.execution_id,
            }))
            return 0
        except Exception as exc:
            print(json.dumps({
                "status": "receipt_error",
                "error_type": type(exc).__name__,
            }))
            return 2

    exit_code, result = run_execution(
        execution_id,
        entrypoint=args.entrypoint,
        force=args.force,
        send=args.send,
        heartbeat_interval=_heartbeat_interval_from_env(),
        max_runtime_seconds=args.timeout_seconds,
    )
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    raise SystemExit(main())
