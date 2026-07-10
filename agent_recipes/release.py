from __future__ import annotations

import json
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, claim_status, now_iso, redact_sensitive_text, sha256_text, write_json


EXPORT_DIR_DEFAULT = "dist/agent-recipes-open-source"
TEXT_SUFFIXES = {".md", ".py", ".txt", ".json", ".yaml", ".yml", ""}
INCLUDE_ROOTS = [".github", "agent_recipes", "bin", "tests"]
INCLUDE_FILES = [
    "pyproject.toml",
    "ENGINEERING_MATURITY.md",
    "ENGINEERING_BUDGET.json",
    "AGENT_RECIPES_COMPETITIVE_STANDARD.md",
    "AGENT_RECIPES_PLAN.md",
    "AGENT_RECIPES_SUPERSET_ROADMAP.md",
    "AGENT_RECIPES_SHRINK_MAP.md",
    "THREE_REPO_COMPETITIVE_SCORECARD.json",
    "THREE_REPO_COMPETITIVE_SCORECARD.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CASS_REPORT_CLEANROOM_LEARNING_MAP.md",
    "references/UPSTREAM_SOURCES.md",
    "requirements-phase2-adapters.txt",
    "requirements-phase2-adapters.lock.txt",
    ".gitignore",
]
EXCLUDED_TOP_LEVEL = {".recipes", ".agents", ".venv", ".git", "dist", "fixtures", "PROJECT_STATUS.md"}


def release_audit(project: Path, *, output_dir: str | Path = EXPORT_DIR_DEFAULT) -> dict[str, Any]:
    root = project.resolve()
    export_root = resolve_export_dir(root, output_dir)
    export_files = planned_export_files(root)
    sanitized_findings = []
    source_findings = []
    patterns = forbidden_patterns(root)
    for rel in export_files:
        path = root / rel
        if not is_text_file(path):
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        source_findings.extend(scan_text(raw, str(rel), patterns))
        source_findings.extend(scan_credential_text(raw, str(rel)))
        sanitized = sanitize_text(raw, root)
        sanitized_findings.extend(scan_text(sanitized, str(rel), patterns))
        sanitized_findings.extend(scan_credential_text(sanitized, str(rel)))
    excluded_present = [name for name in sorted(EXCLUDED_TOP_LEVEL) if (root / name).exists()]
    report = {
        "ok": not sanitized_findings,
        "action": "open-source-audit",
        "checked_at": now_iso(),
        "project_root": str(root),
        "export_dir": str(export_root),
        "included_files": [str(path) for path in export_files],
        "excluded_top_level": excluded_present,
        "source_findings_before_sanitization": source_findings,
        "findings_after_sanitization": sanitized_findings,
        "claim_status": claim_status(
            verified=["已按 allowlist 计算开源导出文件，并扫描脱敏后文本。"],
            missing_evidence=[] if not sanitized_findings else ["脱敏后仍有禁止模式。"],
            cannot_claim=[
                "不能说已经发布到开源仓库。",
                "不能说脱敏质量已人工审过。",
                "不能说项目运行态 .recipes 可以直接公开。",
            ],
        ),
    }
    reports_dir = root / ".recipes" / "reports"
    if reports_dir.exists():
        write_json(reports_dir / "open_source_audit.json", report)
    return report


def export_open_source(project: Path, *, output_dir: str | Path = EXPORT_DIR_DEFAULT) -> dict[str, Any]:
    root = project.resolve()
    export_root = resolve_export_dir(root, output_dir)
    audit = release_audit(root, output_dir=export_root)
    if not audit["ok"]:
        raise RecipesError(
            "AR700",
            "开源导出脱敏检查失败。",
            f"findings={audit['findings_after_sanitization'][:3]}",
            "先修复脱敏规则或从导出 allowlist 移除对应文件。",
        )
    if export_root.exists():
        shutil.rmtree(export_root)
    export_root.mkdir(parents=True, exist_ok=True)
    files_written: list[str] = []
    for rel in planned_export_files(root):
        source = root / rel
        target = export_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_text_file(source):
            target.write_text(sanitize_text(source.read_text(encoding="utf-8", errors="replace"), root), encoding="utf-8")
        else:
            shutil.copy2(source, target)
        if rel.parts[0] == "bin":
            target.chmod(target.stat().st_mode | 0o111)
        files_written.append(str(target))

    readme = export_root / "README.md"
    readme.write_text(open_source_readme(), encoding="utf-8")
    files_written.append(str(readme))

    manifest = {
        "exported_at": now_iso(),
        "source_project": "<sanitized>",
        "files": [str(Path(path).relative_to(export_root)) for path in map(Path, files_written)],
        "excluded": sorted(EXCLUDED_TOP_LEVEL),
        "claim_limits": [
            "This manifest proves a local sanitized export, not its current hosting or publication status.",
            "Runtime state, private fixtures, and local install config are excluded.",
            "Human review is still required before public release.",
        ],
    }
    manifest_path = export_root / "OPEN_SOURCE_MANIFEST.json"
    write_json(manifest_path, manifest)
    files_written.append(str(manifest_path))

    post_findings = scan_export(export_root, forbidden_patterns(root))
    if post_findings:
        raise RecipesError(
            "AR701",
            "导出目录仍包含疑似私有内容。",
            f"findings={post_findings[:3]}",
            "不要发布该目录；先修复脱敏规则。",
        )
    return {
        "ok": True,
        "action": "open-source-export",
        "export_dir": str(export_root),
        "files_written": files_written,
        "file_count": len(files_written),
        "export_hash": sha256_text("\n".join(sorted(file_hash_line(Path(path), export_root) for path in files_written))),
        "claim_status": claim_status(
            verified=["已生成本地开源候选导出，并扫描导出目录禁止模式。"],
            cannot_claim=[
                "不能说已经发布到 GitHub 或插件市场。",
                "不能说脱敏质量已人工审过。",
                "不能说导出包已适合生产安装。",
            ],
        ),
    }


def planned_export_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirname in INCLUDE_ROOTS:
        base = root / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files.append(path.relative_to(root))
    for filename in INCLUDE_FILES:
        path = root / filename
        if path.exists() and path.is_file():
            files.append(Path(filename))
    return sorted(dict.fromkeys(files))


def resolve_export_dir(root: Path, output_dir: str | Path) -> Path:
    path = Path(output_dir)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RecipesError(
            "AR702",
            "开源导出目录必须在项目内。",
            f"output_dir={resolved}",
            "使用项目内路径，例如 dist/agent-recipes-open-source。",
        ) from exc
    return resolved


def is_text_file(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name == "agent-recipes"


def sanitize_text(text: str, root: Path) -> str:
    project_marker = "4" + "J"
    private_project_marker = "4" + "J智能化"
    replacements = {
        str(root): "<project-root>",
        str(root.parent): "<workspace>",
        str(root.home()): "<home>",
        root.home().name: "user",
        private_project_marker: "sample-project",
        project_marker: "SampleProject",
    }
    sanitized = text
    for old, new in replacements.items():
        sanitized = sanitized.replace(old, new)
    sanitized = sanitized.replace("_" + "4" + "j" + "_", "_sample_project_")
    sanitized = sanitized.replace("_" + "4" + "j", "_sample_project")
    sanitized = sanitized.replace("4" + "j" + "_", "sample_project_")
    return sanitized


def forbidden_patterns(root: Path) -> list[tuple[str, str]]:
    project_marker = "4" + "J"
    private_project_marker = "4" + "J智能化"
    return [
        (str(root), "private project path"),
        (str(root.home()), "private home path"),
        (root.home().name, "local username"),
        (private_project_marker, "project-specific private project name"),
        (project_marker, "project-specific fixture name"),
        ("4" + "j" + "_", "project-specific fixture name"),
        ("_" + "4" + "j", "project-specific fixture name"),
    ]


def scan_text(text: str, path: str, patterns: list[tuple[str, str]]) -> list[dict[str, str]]:
    findings = []
    for pattern, reason in patterns:
        if pattern in text:
            findings.append({"path": path, "pattern": pattern, "reason": reason})
    return findings


def scan_credential_text(text: str, path: str) -> list[dict[str, str]]:
    _, report = redact_sensitive_text(text)
    return [
        {"path": path, "pattern": f"credential:{rule}", "reason": "credential-shaped value"}
        for rule in report.get("rules", [])
        if rule != "named_credential"
    ]


def scan_export(export_root: Path, patterns: list[tuple[str, str]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(export_root.rglob("*")):
        if path.is_file() and is_text_file(path):
            text = path.read_text(encoding="utf-8", errors="replace")
            relative = str(path.relative_to(export_root))
            findings.extend(scan_text(text, relative, patterns))
            findings.extend(scan_credential_text(text, relative))
    return findings


def file_hash_line(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    if is_text_file(path):
        content = path.read_text(encoding="utf-8", errors="replace")
    else:
        content = path.read_bytes().hex()
    return f"{rel}:{sha256_text(content)}"


def open_source_readme() -> str:
    return files("agent_recipes").joinpath("OPEN_SOURCE_README.md").read_text(encoding="utf-8")
