from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_recipes.persistence import (
    RecipesError,
    make_id,
    now_iso,
    read_json,
    sha256_json,
    write_json,
    write_text_redacted,
)


DEFAULT_THRESHOLDS = {
    "min_projects": 10,
    "min_latest_self_run_targets": 20,
    "min_cards": 100,
    "min_accepted_review_quality_coverage": 1.0,
}


def evaluate_learning_quality(projects_root: Path, cohort: dict[str, Any]) -> dict[str, Any]:
    projects_root = projects_root.resolve()
    entries = _cohort_entries(cohort)
    thresholds = _thresholds(cohort)
    required_families = _strings(cohort.get("required_families"))
    rows = [_project_row(projects_root, entry) for entry in entries]
    existing = [row for row in rows if row["exists"]]

    latest_self_runs = [item for row in existing for item in row["latest_self_runs"]]
    latest_self_run_failures = [item for item in latest_self_runs if item["ok"] is not True]
    direct_write_failures = [
        item
        for item in latest_self_runs
        if item["no_direct_formal_recipe_write"] is not True
    ]
    card_count = sum(int(row["card_count"]) for row in existing)
    complete_card_count = sum(int(row["complete_card_count"]) for row in existing)
    accepted_reviews = [item for row in existing for item in row["accepted_reviews"]]
    accepted_with_quality = [item for item in accepted_reviews if item["passed_quality_evidence"]]
    accepted_quality_gaps = [item for item in accepted_reviews if not item["passed_quality_evidence"]]
    present_families = sorted(
        {
            str(row["family"])
            for row in existing
            if row.get("family") and int(row["self_run_report_count"]) > 0
        }
    )
    missing_families = [family for family in required_families if family not in present_families]
    card_contract_rate = _ratio(complete_card_count, card_count)
    accepted_quality_coverage = _ratio(len(accepted_with_quality), len(accepted_reviews))

    gates = [
        _gate(
            "cohort_projects_exist",
            len(existing) == len(rows),
            actual=len(existing),
            required=len(rows),
            detail="样本清单中的项目必须全部存在。",
        ),
        _gate(
            "required_source_families_present",
            not missing_families,
            actual=present_families,
            required=required_families,
            detail="纠偏、课程、产物等指定来源类型必须都有真实样本。",
        ),
        _gate(
            "minimum_project_count",
            len(existing) >= thresholds["min_projects"],
            actual=len(existing),
            required=thresholds["min_projects"],
            detail="不能用少数项目冒充大样本。",
        ),
        _gate(
            "minimum_latest_self_run_targets",
            len(latest_self_runs) >= thresholds["min_latest_self_run_targets"],
            actual=len(latest_self_runs),
            required=thresholds["min_latest_self_run_targets"],
            detail="按项目和 target 只看最后一次 self-run。",
        ),
        _gate(
            "latest_self_runs_pass",
            not latest_self_run_failures,
            actual=len(latest_self_run_failures),
            required=0,
            detail="同一 target 的最后一次系统自跑不能仍然失败。",
        ),
        _gate(
            "no_direct_formal_recipe_write",
            not direct_write_failures,
            actual=len(direct_write_failures),
            required=0,
            detail="self-run 必须停在 review queue，不能直写正式菜谱。",
        ),
        _gate(
            "minimum_card_count",
            card_count >= thresholds["min_cards"],
            actual=card_count,
            required=thresholds["min_cards"],
            detail="候选卡数量必须达到样本门槛。",
        ),
        _gate(
            "all_cards_have_candidate_contract",
            card_count > 0 and complete_card_count == card_count,
            actual={"complete": complete_card_count, "total": card_count, "rate": card_contract_rate},
            required={"rate": 1.0},
            detail="每张卡都必须有 source_trace、target_fields、candidate 强度和 cannot_claim。",
        ),
        _gate(
            "accepted_reviews_have_passed_quality_evidence",
            accepted_quality_coverage >= thresholds["min_accepted_review_quality_coverage"],
            actual={
                "covered": len(accepted_with_quality),
                "total": len(accepted_reviews),
                "coverage": accepted_quality_coverage,
            },
            required=thresholds["min_accepted_review_quality_coverage"],
            detail="已经接受的候选必须有通过的 candidate-quality case，不能只靠技术链通过。",
        ),
    ]
    failed_gates = [gate for gate in gates if gate["status"] == "failed"]
    missing_projects = [str(row["project_name"]) for row in rows if not row["exists"]]
    gaps = [
        {
            "project": item["project_name"],
            "review_id": item["review_id"],
            "target_recipe_id": item["target_recipe_id"],
            "gap": "accepted_review_without_passed_candidate_quality",
        }
        for item in accepted_quality_gaps
    ]
    gaps.extend(
        {
            "project": item["project_name"],
            "target_recipe_id": item["target_recipe_id"],
            "gap": "latest_self_run_failed",
        }
        for item in latest_self_run_failures
    )
    gaps.extend(
        {
            "project": item["project_name"],
            "target_recipe_id": item["target_recipe_id"],
            "gap": "self_run_direct_write_guard_failed_or_missing",
        }
        for item in direct_write_failures
    )

    return {
        "ok": not failed_gates,
        "action": "learning-quality-summary",
        "status": "passed" if not failed_gates else "failed",
        "candidate_only": True,
        "cohort_id": str(cohort.get("cohort_id") or "unnamed_cohort"),
        "projects_root": str(projects_root),
        "thresholds": thresholds,
        "summary": {
            "cohort_project_count": len(rows),
            "existing_project_count": len(existing),
            "projects_with_self_run": sum(1 for row in existing if row["self_run_report_count"] > 0),
            "self_run_report_count": sum(int(row["self_run_report_count"]) for row in existing),
            "latest_self_run_target_count": len(latest_self_runs),
            "latest_self_run_target_failure_count": len(latest_self_run_failures),
            "card_count": card_count,
            "complete_card_count": complete_card_count,
            "card_contract_rate": card_contract_rate,
            "patch_draft_count": sum(int(row["patch_draft_count"]) for row in existing),
            "candidate_quality_report_count": sum(int(row["candidate_quality_report_count"]) for row in existing),
            "candidate_quality_case_count": sum(int(row["candidate_quality_case_count"]) for row in existing),
            "candidate_quality_passed_case_count": sum(int(row["candidate_quality_passed_case_count"]) for row in existing),
            "candidate_quality_failed_case_count": sum(int(row["candidate_quality_failed_case_count"]) for row in existing),
            "review_count": sum(int(row["review_count"]) for row in existing),
            "accepted_review_count": len(accepted_reviews),
            "rejected_review_count": sum(int(row["rejected_review_count"]) for row in existing),
            "pending_review_count": sum(int(row["pending_review_count"]) for row in existing),
            "accepted_reviews_with_passed_quality": len(accepted_with_quality),
            "accepted_review_quality_gap_count": len(accepted_quality_gaps),
            "accepted_review_quality_coverage": accepted_quality_coverage,
            "failed_gate_count": len(failed_gates),
        },
        "present_families": present_families,
        "missing_required_families": missing_families,
        "missing_projects": missing_projects,
        "gates": gates,
        "quality_gaps": gaps,
        "projects": rows,
        "production_notes": [
            "这个总门只读取已有 self-run、candidate-quality、cards 和 review 证据。",
            "它不重新拆资料，不生成候选，不接受 review，也不修改正式 recipe。",
            "历史失败保留在计数里；同一项目和 target 的当前门只看最后一次 self-run。",
            "通过仍不证明 Agent 已掌握技能、真实任务已执行或最终产物质量合格。",
        ],
    }


def run_learning_quality_summary(
    project: Any,
    *,
    projects_root: str,
    cohort_path: str,
) -> dict[str, Any]:
    project.ensure_dirs()
    root = _resolve(project.root, projects_root)
    cohort_file = _resolve(project.root, cohort_path)
    if not root.is_dir():
        raise RecipesError("AR480", "learning-quality-summary projects root 不存在。", str(root), "传入存在的 --projects-root。")
    if not cohort_file.is_file():
        raise RecipesError("AR481", "learning-quality-summary cohort 文件不存在。", str(cohort_file), "传入存在的 --cohort JSON。")
    cohort = read_json(cohort_file, None)
    if not isinstance(cohort, dict):
        raise RecipesError("AR482", "learning-quality-summary cohort 必须是 JSON object。", str(cohort_file), "写入 projects、required_families 和 thresholds。")

    report = evaluate_learning_quality(root, cohort)
    report["cohort_path"] = str(cohort_file)
    report_hash = sha256_json(report)
    report_id = make_id("learning_quality", report_hash)
    report_path = project.recipes_dir / "reports" / f"{report_id}.json"
    markdown_path = project.recipes_dir / "reports" / f"{report_id}.md"
    report.update(
        {
            "report_id": report_id,
            "report_hash": report_hash,
            "report_path": str(report_path),
            "markdown_path": str(markdown_path),
            "checked_at": now_iso(),
        }
    )
    write_json(report_path, report)
    write_text_redacted(markdown_path, learning_quality_markdown(report))
    claim = {
        "verified": [
            "已按固定 cohort 汇总系统自跑、候选卡、候选质量和审核证据。",
            "已检查 self-run 直写正式 recipe 防线和 accepted review 质量证据覆盖。",
        ],
        "inferred": [],
        "missing_evidence": [gate["detail"] for gate in report["gates"] if gate["status"] == "failed"],
        "cannot_claim": [
            "不能说 learning-quality-summary 通过就证明 Agent 已学会技能。",
            "不能说候选质量通过就证明真实任务执行或最终产物质量合格。",
            "不能说汇总报告替代人工 review。",
        ],
    }
    event, idem = project.append_event(
        "learning_quality_summary_ran",
        {
            "report_id": report_id,
            "report_hash": report_hash,
            "cohort_id": report["cohort_id"],
            "status": report["status"],
            "failed_gate_count": report["summary"]["failed_gate_count"],
        },
        idempotency_key=f"learning-quality-summary:{report_hash}",
        lock_exempt_reason="local_learning_quality_summary",
        claim_status=claim,
    )
    result = {
        "action": "learning-quality-summary",
        "idempotency_status": idem,
        "files_written": [str(report_path), str(markdown_path), str(project.events_path)],
        "objects_created": [report_id, event["event_id"]] if idem == "created" else [],
        "objects_updated": [],
        "previous_hash": None,
        "new_hash": None,
        "rollback": "汇总报告和事件为追加证据；不删除历史，用后续报告替代当前判断。",
        "claim_status": claim,
    }
    result.update(report)
    return result


def learning_quality_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 大样本学习质量总门",
        "",
        f"Report: `{report.get('report_id', '')}`",
        "",
        "## 一眼结论",
        "",
        f"- 状态：`{report['status']}`",
        f"- cohort 项目：`{summary['existing_project_count']}/{summary['cohort_project_count']}`",
        f"- 最后一次 self-run target：`{summary['latest_self_run_target_count']}`，失败：`{summary['latest_self_run_target_failure_count']}`",
        f"- 候选卡合同完整：`{summary['complete_card_count']}/{summary['card_count']}`",
        f"- 已接受候选有质量证据：`{summary['accepted_reviews_with_passed_quality']}/{summary['accepted_review_count']}`",
        f"- 当前质量缺口：`{len(report['quality_gaps'])}`",
        "- 这个裁判不拆资料、不接受 review、不改正式菜谱。",
        "",
        "## 硬门",
        "",
    ]
    for gate in report["gates"]:
        mark = "PASS" if gate["status"] == "passed" else "FAIL"
        lines.append(f"- `{mark}` `{gate['gate_id']}`：{gate['detail']} actual=`{gate['actual']}` required=`{gate['required']}`")
    lines.extend(["", "## 质量缺口", ""])
    if not report["quality_gaps"]:
        lines.append("- 当前 cohort 没有汇总到质量缺口。")
    else:
        for gap in report["quality_gaps"]:
            lines.append(
                f"- `{gap.get('project')}`：`{gap.get('gap')}`"
                + (f"，review=`{gap.get('review_id')}`" if gap.get("review_id") else "")
                + (f"，target=`{gap.get('target_recipe_id')}`" if gap.get("target_recipe_id") else "")
            )
    lines.extend(
        [
            "",
            "## 不能声称",
            "",
            "- 不能说这份报告证明 Agent 已经学会课程或技能。",
            "- 不能说候选质量通过等于真实任务或最终产物质量通过。",
            "- 不能用这份汇总替代人工 review。",
            "",
        ]
    )
    return "\n".join(lines)


def _project_row(projects_root: Path, entry: dict[str, str]) -> dict[str, Any]:
    project_name = entry["name"]
    family = entry["family"]
    recipes_dir = projects_root / project_name / ".recipes"
    if not recipes_dir.is_dir():
        return {
            "project_name": project_name,
            "family": family,
            "exists": False,
            "latest_self_runs": [],
            "self_run_report_count": 0,
            "card_count": 0,
            "complete_card_count": 0,
            "patch_draft_count": 0,
            "candidate_quality_report_count": 0,
            "candidate_quality_case_count": 0,
            "candidate_quality_passed_case_count": 0,
            "candidate_quality_failed_case_count": 0,
            "review_count": 0,
            "accepted_reviews": [],
            "rejected_review_count": 0,
            "pending_review_count": 0,
        }

    self_reports = _json_docs(recipes_dir / "reports", "self_run_*.json")
    latest_self_runs = _latest_self_runs(project_name, self_reports)
    cards = _card_docs(recipes_dir)
    complete_cards = [card for card in cards if _card_contract_complete(card)]
    quality_reports = _json_docs(recipes_dir / "reports", "candidate_quality_*.json")
    quality_cases = [case for report in quality_reports for case in _dicts(report.get("cases"))]
    passed_review_ids = {
        str(case.get("review_id") or case.get("expected_review_id"))
        for case in quality_cases
        if case.get("status") == "passed" and (case.get("review_id") or case.get("expected_review_id"))
    }
    review_docs = [doc for doc in _json_docs(recipes_dir / "review_queue", "*.json") if doc.get("review_id")]
    accepted_by_target: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    for review in review_docs:
        if review.get("status") != "accepted":
            continue
        target = str(review.get("target_recipe_id") or review.get("recipe_id") or review.get("review_id"))
        stamp = (str(review.get("decided_at") or ""), str(review.get("review_id") or ""))
        if target not in accepted_by_target or stamp > accepted_by_target[target][0]:
            accepted_by_target[target] = (stamp, review)
    accepted_reviews = [
        {
            "project_name": project_name,
            "review_id": str(review.get("review_id")),
            "target_recipe_id": target,
            "passed_quality_evidence": str(review.get("review_id")) in passed_review_ids,
        }
        for target, (_, review) in sorted(accepted_by_target.items())
    ]
    return {
        "project_name": project_name,
        "family": family,
        "exists": True,
        "latest_self_runs": latest_self_runs,
        "self_run_report_count": len(self_reports),
        "card_count": len(cards),
        "complete_card_count": len(complete_cards),
        "patch_draft_count": len(_json_docs(recipes_dir / "source_refinery" / "patch_drafts", "patch_draft_*.json")),
        "candidate_quality_report_count": len(quality_reports),
        "candidate_quality_case_count": len(quality_cases),
        "candidate_quality_passed_case_count": sum(1 for case in quality_cases if case.get("status") == "passed"),
        "candidate_quality_failed_case_count": sum(1 for case in quality_cases if case.get("status") != "passed"),
        "review_count": len(review_docs),
        "accepted_reviews": accepted_reviews,
        "rejected_review_count": sum(1 for review in review_docs if review.get("status") == "rejected"),
        "pending_review_count": sum(1 for review in review_docs if review.get("status") == "pending"),
    }


def _latest_self_runs(project_name: str, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    for report in reports:
        target = str(report.get("target_recipe_id") or report.get("review_id") or report.get("report_id") or "unknown")
        stamp = (str(report.get("checked_at") or ""), str(report.get("report_path") or report.get("report_id") or ""))
        if target not in latest or stamp > latest[target][0]:
            latest[target] = (stamp, report)
    rows: list[dict[str, Any]] = []
    for target, (_, report) in sorted(latest.items()):
        cases = _dicts(report.get("cases"))
        direct_case = next((case for case in cases if case.get("case_id") == "no_direct_formal_recipe_write"), None)
        rows.append(
            {
                "project_name": project_name,
                "target_recipe_id": target,
                "report_id": report.get("report_id"),
                "ok": report.get("ok"),
                "no_direct_formal_recipe_write": bool(direct_case and direct_case.get("status") == "passed"),
            }
        )
    return rows


def _cohort_entries(cohort: dict[str, Any]) -> list[dict[str, str]]:
    raw = cohort.get("projects")
    if not isinstance(raw, list) or not raw:
        raise ValueError("cohort projects must be a non-empty list")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("cohort project entry must be an object")
        name = str(item.get("name") or "").strip()
        family = str(item.get("family") or "").strip()
        if not name or not family:
            raise ValueError("cohort project requires name and family")
        if name in seen:
            raise ValueError(f"duplicate cohort project: {name}")
        seen.add(name)
        entries.append({"name": name, "family": family})
    return entries


def _thresholds(cohort: dict[str, Any]) -> dict[str, float | int]:
    raw = cohort.get("thresholds") if isinstance(cohort.get("thresholds"), dict) else {}
    thresholds: dict[str, float | int] = dict(DEFAULT_THRESHOLDS)
    for key in ("min_projects", "min_latest_self_run_targets", "min_cards"):
        value = raw.get(key, thresholds[key])
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{key} must be a positive integer")
        thresholds[key] = value
    coverage = raw.get("min_accepted_review_quality_coverage", thresholds["min_accepted_review_quality_coverage"])
    if not isinstance(coverage, (int, float)) or isinstance(coverage, bool) or coverage < 0 or coverage > 1:
        raise ValueError("min_accepted_review_quality_coverage must be between 0 and 1")
    thresholds["min_accepted_review_quality_coverage"] = float(coverage)
    return thresholds


def _json_docs(directory: Path, pattern: str) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    docs: list[dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        if path.name == "latest.json":
            continue
        value = read_json(path, None)
        if isinstance(value, dict):
            value.setdefault("report_path", str(path))
            docs.append(value)
    return docs


def _card_docs(recipes_dir: Path) -> list[dict[str, Any]]:
    cards_dir = recipes_dir / "source_refinery" / "cards"
    if not cards_dir.is_dir():
        return []
    docs: list[dict[str, Any]] = []
    for path in sorted(cards_dir.glob("*/*.json")):
        value = read_json(path, None)
        if isinstance(value, dict) and value.get("card_id"):
            docs.append(value)
    return docs


def _card_contract_complete(card: dict[str, Any]) -> bool:
    return bool(
        _dicts(card.get("source_trace"))
        and _strings(card.get("target_fields"))
        and card.get("evidence_strength") == "candidate"
        and _strings(card.get("cannot_claim"))
    )


def _gate(gate_id: str, passed: bool, *, actual: Any, required: Any, detail: str) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "actual": actual,
        "required": required,
        "detail": detail,
    }


def _resolve(project_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []
