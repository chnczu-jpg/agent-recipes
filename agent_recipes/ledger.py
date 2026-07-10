from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Callable

from agent_recipes.persistence import (
    RecipesError,
    annotate_persistence_redaction,
    append_jsonl,
    make_id,
    now_iso,
    read_jsonl,
    redact_sensitive_text,
    redact_sensitive_value,
    sha256_json,
)


EVENT_SCHEMA_VERSION = "0.1"


class EventLedger:
    """Append-only event ledger with one small interface and fail-closed writes."""

    def __init__(self, events_path: Path, ensure_storage: Callable[[], object]):
        self.events_path = events_path
        self._ensure_storage = ensure_storage

    def load(self) -> list[dict[str, Any]]:
        return read_jsonl(self.events_path)

    @staticmethod
    def event_hash(event: dict[str, Any]) -> str:
        without_hash = {key: value for key, value in event.items() if key != "event_hash"}
        return sha256_json(without_hash)

    def inspect(self) -> dict[str, Any]:
        try:
            events = self.load()
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "events": [],
                "errors": [{"code": "AR299", "message": f"events.jsonl 无法解析：{exc}"}],
            }
        errors = self._chain_errors(events)
        return {"ok": not errors, "events": events, "errors": errors}

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "codex",
        session_id: str | None = None,
        lock_id: str | None = None,
        lock_exempt_reason: str | None = None,
        causation_id: str | None = None,
        idempotency_key: str | None = None,
        claim_status: dict[str, list[str]] | None = None,
    ) -> tuple[dict[str, Any], str]:
        self._ensure_storage()
        payload, redaction_report = redact_sensitive_value(payload)
        payload = annotate_persistence_redaction(payload, redaction_report)
        actor, _ = redact_sensitive_text(actor)
        session_value, _ = redact_sensitive_text(session_id or os.environ.get("AGENT_RECIPES_SESSION_ID", "local"))
        lock_id = redact_sensitive_text(lock_id)[0] if lock_id else None
        causation_id = redact_sensitive_text(causation_id)[0] if causation_id else None
        idempotency_key = redact_sensitive_text(idempotency_key)[0] if idempotency_key else None
        safe_claim_status, claim_redaction_report = redact_sensitive_value(claim_status or empty_claim_status())
        safe_claim_status = annotate_persistence_redaction(safe_claim_status, claim_redaction_report)

        lock_path = self.events_path.parent / ".events.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    events = self.load()
                except (OSError, json.JSONDecodeError) as exc:
                    raise RecipesError(
                        "AR405",
                        "事件账本不可读，拒绝继续写入。",
                        str(exc),
                        "先恢复 events.jsonl，再运行 doctor。",
                    ) from exc
                chain_errors = self._chain_errors(events)
                if chain_errors:
                    raise RecipesError(
                        "AR405",
                        "事件账本已损坏，拒绝继续写入。",
                        chain_errors[0]["message"],
                        "先恢复 events.jsonl 并让 doctor 通过，再执行写命令。",
                    )

                payload_hash = sha256_json(payload)
                key = idempotency_key or f"{event_type}:{payload_hash}"
                for existing in events:
                    if existing.get("idempotency_key") == key:
                        if existing.get("payload_hash") == payload_hash:
                            return existing, "replayed"
                        raise RecipesError(
                            "AR409",
                            "幂等 key 冲突。",
                            "同一个 idempotency_key 被不同 payload 复用。",
                            "重新读取最新 events.jsonl，并换一个 idempotency key。",
                        )

                prev_hash = events[-1]["event_hash"] if events else None
                event: dict[str, Any] = {
                    "event_id": make_id("evt", event_type, key, payload_hash),
                    "event_type": event_type,
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "created_at": now_iso(),
                    "actor": actor,
                    "session_id": session_value,
                    "lock_id": lock_id,
                    "causation_id": causation_id,
                    "idempotency_key": key,
                    "payload_hash": payload_hash,
                    "prev_event_hash": prev_hash,
                    "seq": len(events) + 1,
                    "payload": payload,
                    "claim_status": safe_claim_status,
                }
                if lock_exempt_reason:
                    event["lock_exempt_reason"] = lock_exempt_reason
                event["event_hash"] = self.event_hash(event)
                append_jsonl(self.events_path, event)
                return event, "created"
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _chain_errors(self, events: list[dict[str, Any]]) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        prev_hash = None
        for index, event in enumerate(events, start=1):
            if event.get("seq") != index:
                errors.append({"code": "AR300", "message": f"event seq 不连续：{event.get('event_id')}"})
            if event.get("prev_event_hash") != prev_hash:
                errors.append({"code": "AR301", "message": f"event prev_event_hash 不匹配：{event.get('event_id')}"})
            if self.event_hash(event) != event.get("event_hash"):
                errors.append({"code": "AR302", "message": f"event_hash 不匹配：{event.get('event_id')}"})
            if not event.get("lock_id") and not event.get("lock_exempt_reason"):
                errors.append({"code": "AR303", "message": f"mutation event 没有 lock_id 或 lock_exempt_reason：{event.get('event_id')}"})
            prev_hash = event.get("event_hash")
        return errors


def empty_claim_status() -> dict[str, list[str]]:
    return {"verified": [], "inferred": [], "missing_evidence": [], "cannot_claim": []}
