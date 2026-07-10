from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RecipesError(Exception):
    def __init__(self, code: str, problem: str, cause: str, fix_command: str = ""):
        super().__init__(problem)
        self.code = code
        self.problem = problem
        self.cause = cause
        self.fix_command = fix_command

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "problem": self.problem,
            "cause": self.cause,
            "fix_command": self.fix_command,
            "files_changed": [],
            "claim_status": {
                "verified": [],
                "inferred": [],
                "missing_evidence": [self.cause],
                "cannot_claim": ["不能说命令已成功执行。"],
            },
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(data: Any) -> str:
    return sha256_text(stable_json(data))


def make_id(prefix: str, *parts: Any) -> str:
    digest = sha256_json(parts)[:12]
    return f"{prefix}_{digest}"


SECRET_PATTERNS = (
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|password)\b"
    r"\s*[:=]\s*([\"']?)([A-Za-z0-9_./+=:-]{12,})\2"
)
SENSITIVE_PERSISTENCE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "password",
    "private_key",
    "secret",
    "token",
}
PERSISTENCE_SAFE_KEY_SUFFIXES = ("_env", "_hash", "_path", "_present", "_stored")


def looks_like_env_reference(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{5,}", value.strip()))


def redact_sensitive_text(text: str) -> tuple[str, dict[str, Any]]:
    redacted = text
    counts: dict[str, int] = {}
    for rule_name, pattern in SECRET_PATTERNS:
        redacted, count = pattern.subn(f"[REDACTED:{rule_name}]", redacted)
        if count:
            counts[rule_name] = counts.get(rule_name, 0) + count

    def assignment_replacement(match: re.Match[str]) -> str:
        value = match.group(3)
        if looks_like_env_reference(value):
            return match.group(0)
        counts["named_credential"] = counts.get("named_credential", 0) + 1
        return f"{match.group(1)}=[REDACTED:named_credential]"

    redacted = SECRET_ASSIGNMENT_RE.sub(assignment_replacement, redacted)
    return redacted, {
        "count": sum(counts.values()),
        "rules": sorted(counts),
        "counts": counts,
    }


def redact_sensitive_value(value: Any, *, key_hint: str = "") -> tuple[Any, dict[str, Any]]:
    counts: dict[str, int] = {}

    def add_report(report: dict[str, Any]) -> None:
        for name, count in report.get("counts", {}).items():
            counts[name] = counts.get(name, 0) + int(count)

    normalized_key = key_hint.strip().casefold().replace("-", "_").replace(" ", "_")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            child_key = str(key)
            normalized_child_key = child_key.strip().casefold().replace("-", "_").replace(" ", "_")
            if (
                normalized_child_key in SENSITIVE_PERSISTENCE_KEYS
                and not normalized_child_key.endswith(PERSISTENCE_SAFE_KEY_SUFFIXES)
                and isinstance(child, str)
                and child
                and not looks_like_env_reference(child)
            ):
                result[child_key] = "[REDACTED:sensitive_field]"
                counts["sensitive_field"] = counts.get("sensitive_field", 0) + 1
                continue
            safe_child, report = redact_sensitive_value(child, key_hint=child_key)
            result[child_key] = safe_child
            add_report(report)
        safe_value: Any = result
    elif isinstance(value, list):
        result_list: list[Any] = []
        for child in value:
            safe_child, report = redact_sensitive_value(child, key_hint=normalized_key)
            result_list.append(safe_child)
            add_report(report)
        safe_value = result_list
    elif isinstance(value, tuple):
        result_tuple: list[Any] = []
        for child in value:
            safe_child, report = redact_sensitive_value(child, key_hint=normalized_key)
            result_tuple.append(safe_child)
            add_report(report)
        safe_value = result_tuple
    elif isinstance(value, str):
        safe_value, report = redact_sensitive_text(value)
        add_report(report)
    else:
        safe_value = value
    return safe_value, {
        "count": sum(counts.values()),
        "rules": sorted(counts),
        "counts": counts,
    }


def annotate_persistence_redaction(value: Any, report: dict[str, Any]) -> Any:
    if not report.get("count") or not isinstance(value, dict):
        return value
    annotated = dict(value)
    annotated.setdefault(
        "persistence_redaction",
        {"applied": True, "count": report["count"], "rules": report["rules"]},
    )
    return annotated


def authoritative_persistence_path(path: Path) -> bool:
    parts = set(path.parts)
    return bool({"recipes", "locks"} & parts) and ".recipes" in parts


def prepare_persistence_value(path: Path, value: Any) -> Any:
    safe_value, report = redact_sensitive_value(value)
    if report.get("count") and authoritative_persistence_path(path):
        raise RecipesError(
            "AR450",
            "正式 recipe 或 lock 含敏感凭证，拒绝写盘。",
            f"path={path}; redaction_rules={report['rules']}",
            "先把凭证改成环境变量名，再重新 review/lock。",
        )
    return annotate_persistence_redaction(safe_value, report)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = prepare_persistence_value(path, data)
    tmp = temporary_sibling(path)
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def read_optional_json(path: Path, default: Any) -> tuple[Any, str | None]:
    try:
        return read_json(path, default), None
    except (OSError, json.JSONDecodeError) as exc:
        return default, f"{path}: {exc}"


def read_optional_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        return read_jsonl(path), None
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"{path}: {exc}"


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = prepare_persistence_value(path, data)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [prepare_persistence_value(path, row) for row in rows]
    tmp = temporary_sibling(path)
    try:
        tmp.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def temporary_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def write_text_redacted(path: Path, text: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_text, report = redact_sensitive_text(text)
    if report.get("count") and authoritative_persistence_path(path):
        raise RecipesError(
            "AR450",
            "正式 recipe 或 lock 含敏感凭证，拒绝写盘。",
            f"path={path}; redaction_rules={report['rules']}",
            "先把凭证改成环境变量名再写入。",
        )
    tmp = temporary_sibling(path)
    try:
        tmp.write_text(safe_text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return report


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
