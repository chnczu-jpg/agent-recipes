from __future__ import annotations

import json
import math
import resource
import time
from pathlib import Path
from typing import Any, Callable

from agent_recipes.execution import (
    active_recipe_ids_for_consumption,
    load_lookup_priority_rules,
    lookup_applicability,
    rank_recipes_for_lookup,
    recall_no_match_reason,
    recipe_lookup_haystack,
)
from agent_recipes.lifecycle import recipe_lifecycle_state
from agent_recipes.persistence import (
    RecipesError,
    make_id,
    now_iso,
    read_json,
    sha256_json,
    stable_json,
    write_json,
)


BACKENDS = ("core", "cognee", "graphiti", "qwen")


def run_recall_quality_benchmark(
    project: Any,
    *,
    cases_path: str,
    backends: list[str],
    allow_loopback: bool,
    limit: int,
    min_score: int,
    qwen_min_score: float,
    timeout: int,
    candidate_ranker: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]],
    embedding_caller: Callable[[dict[str, Any], str], list[float]],
    embedding_config_reader: Callable[[Path], dict[str, Any]],
    cosine_similarity: Callable[[list[float], list[float]], float],
) -> dict[str, Any]:
    if limit < 1 or min_score < 1 or timeout < 1:
        raise RecipesError("AR700", "recall quality 参数必须大于 0。", f"limit={limit}; min_score={min_score}; timeout={timeout}")
    if not 0.0 <= qwen_min_score <= 1.0:
        raise RecipesError("AR700", "Qwen no-match 阈值必须在 0 到 1 之间。", str(qwen_min_score))
    selected_backends = list(dict.fromkeys(str(item).strip().casefold() for item in backends if str(item).strip()))
    unsupported = [item for item in selected_backends if item not in BACKENDS]
    if not selected_backends or unsupported:
        raise RecipesError("AR700", "recall quality backend 不支持。", f"backends={selected_backends}; unsupported={unsupported}")

    resolved_cases_path = _resolve_file(project.root, cases_path)
    raw_cases = read_json(resolved_cases_path, {})
    cases = raw_cases.get("cases") if isinstance(raw_cases, dict) else raw_cases
    if not isinstance(cases, list) or not cases:
        raise RecipesError("AR701", "recall quality cases 必须是非空列表。", str(resolved_cases_path))
    normalized_case_rows = [_normalize_case(item, index) for index, item in enumerate(cases)]
    normalized_cases = _dedupe_cases(normalized_case_rows)

    all_recipes = project.load_recipes()
    lifecycle = recipe_lifecycle_state(project.load_events())
    active_ids, inactive_ids = active_recipe_ids_for_consumption(
        all_recipes,
        retired_recipe_ids=set(lifecycle["retired_recipe_ids"]),
    )
    recipes = [recipe for recipe in all_recipes if str(recipe.get("recipe_id") or "") in active_ids]
    recipes.sort(key=lambda item: str(item.get("recipe_id") or ""))
    if not recipes:
        raise RecipesError("AR702", "没有可用于 recall quality 的 active recipe。", str(project.recipes_dir / "recipes"))
    recipe_ids = [str(recipe["recipe_id"]) for recipe in recipes]
    missing_expected = sorted(
        {
            str(case["expected_recipe_id"])
            for case in normalized_cases
            if case.get("expected_recipe_id") and str(case["expected_recipe_id"]) not in set(recipe_ids)
        }
    )
    if missing_expected:
        raise RecipesError(
            "AR703",
            "题库期望的 recipe 不在本次统一候选池。",
            stable_json(missing_expected),
            "使用包含全部 expected_recipe_id 的项目运行，不能拿不同语料硬比较。",
        )

    corpus = _project_recipes(recipes)
    corpus_hash = sha256_json([{"recipe_id": item["recipe_id"], "text": item["text"]} for item in corpus])
    case_hash = sha256_json(normalized_cases)
    report_backends: dict[str, Any] = {}
    peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    benchmark_started = time.perf_counter()

    if "core" in selected_backends:
        report_backends["core"] = _run_core(
            project,
            normalized_cases,
            recipes,
            priority_rules=load_lookup_priority_rules(project.recipes_dir),
            limit=limit,
            min_score=min_score,
        )
    if "cognee" in selected_backends:
        report_backends["cognee"] = _run_candidate_projection(
            "cognee",
            normalized_cases,
            corpus,
            candidate_ranker=candidate_ranker,
            limit=limit,
        )
    if "graphiti" in selected_backends:
        graph_records = _graph_projection(corpus)
        report_backends["graphiti"] = _run_candidate_projection(
            "graphiti",
            normalized_cases,
            graph_records,
            candidate_ranker=candidate_ranker,
            limit=limit,
        )
        report_backends["graphiti"]["projected_record_count"] = len(graph_records)
    if "qwen" in selected_backends:
        if not allow_loopback:
            raise RecipesError(
                "AR704",
                "Qwen 同语料基准要求真实本地服务，但没有显式允许 loopback。",
                "缺少 --allow-loopback。",
                "启动项目内已配置的 Qwen 服务后重跑；不能用旧 status 冒充本轮证据。",
            )
        config = embedding_config_reader(project.root)
        if config.get("config_status") != "configured":
            raise RecipesError("AR704", "Qwen embedding 尚未配置。", "找不到当前项目的 qwen3 config。")
        report_backends["qwen"] = _run_qwen(
            project,
            normalized_cases,
            corpus,
            corpus_hash=corpus_hash,
            config=config,
            embedding_caller=embedding_caller,
            cosine_similarity=cosine_similarity,
            limit=limit,
            min_score=qwen_min_score,
        )

    elapsed_ms = round((time.perf_counter() - benchmark_started) * 1000.0, 3)
    peak_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    comparable = all(report_backends.get(name, {}).get("same_corpus") for name in selected_backends)
    backend_gate_passed = all(report_backends[name]["gate"]["passed"] for name in selected_backends)
    failed_backend_names = [name for name in selected_backends if not report_backends[name]["gate"]["passed"]]
    report = {
        "ok": comparable and backend_gate_passed,
        "action": "recall-quality-benchmark",
        "benchmark_scope": "same_corpus_projection",
        "read_only_inputs": True,
        "candidate_only": True,
        "same_corpus": comparable,
        "corpus": {
            "recipe_count": len(recipes),
            "recipe_ids": recipe_ids,
            "corpus_hash": corpus_hash,
            "inactive_recipe_ids": inactive_ids,
        },
        "case_cohort": {
            "path": str(resolved_cases_path),
            "source_row_count": len(normalized_case_rows),
            "case_count": len(normalized_cases),
            "duplicate_row_count": len(normalized_case_rows) - len(normalized_cases),
            "scoring_policy": "exact duplicate expectations are scored once",
            "positive_count": sum(bool(item["expect_applicable"]) for item in normalized_cases),
            "pure_no_match_count": sum(_case_kind(item) == "pure_no_match" for item in normalized_cases),
            "target_overreach_count": sum(_case_kind(item) == "target_overreach" for item in normalized_cases),
            "case_hash": case_hash,
        },
        "backends": report_backends,
        "gate": {
            "passed": comparable and backend_gate_passed,
            "all_backends_passed": backend_gate_passed,
            "policy": {
                "minimum_top1_accuracy": 0.8,
                "minimum_pure_no_match_accuracy": 0.9,
                "maximum_false_recall_rate": 0.1,
            },
        },
        "resource_use": {
            "wall_time_ms": elapsed_ms,
            "process_peak_rss_before": peak_before,
            "process_peak_rss_after": peak_after,
            "process_peak_rss_unit": "platform_native_ru_maxrss",
        },
        "claim_status": {
            "verified": [
                f"已在同一批 {len(recipes)} 条 active recipe、{len(normalized_cases)} 道唯一题上运行 recall quality benchmark；原始题库 {len(normalized_case_rows)} 行。",
                "四条链只比较临时同语料投影；正式 recipe、候选索引和 review 状态未被修改。",
            ],
            "inferred": [],
            "missing_evidence": (
                ([] if comparable else ["至少一个 backend 没有完成同语料比较。"])
                + [f"{name} 没有通过固定同语料门。" for name in failed_backend_names]
            ),
            "cannot_claim": [
                "不能说 benchmark 投影等于 Cognee 或 Graphiti 的完整原生运行时。",
                "不能说一次固定题库通过就证明任意领域召回质量。",
                "不能说 candidate recall 可以绕过 strict lookup、execution lock 或 review_queue。",
                "不能把旧 status 或旧索引当成本轮 Qwen 真实调用证据。",
            ],
        },
    }
    report_hash = sha256_json(report)
    report_id = make_id("recall_quality", report_hash)
    report_path = project.recipes_dir / "reports" / f"{report_id}.json"
    report["report_id"] = report_id
    report["report_hash"] = report_hash
    report["report_path"] = str(report_path)
    report["checked_at"] = now_iso()
    write_json(report_path, report)
    event, idem = project.append_event(
        "recall_quality_benchmark_ran",
        {
            "report_id": report_id,
            "report_hash": report_hash,
            "corpus_hash": corpus_hash,
            "case_hash": case_hash,
            "backends": selected_backends,
            "same_corpus": comparable,
            "gate_passed": report["gate"]["passed"],
        },
        idempotency_key=f"recall-quality-benchmark:{report_hash}",
        lock_exempt_reason="read_only_recall_quality_benchmark",
        claim_status=report["claim_status"],
    )
    report["idempotency_status"] = idem
    report["events"] = [event["event_id"]] if idem == "created" else []
    report["files_written"] = [str(report_path), str(project.events_path)]
    return report


def _resolve_file(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RecipesError("AR701", "recall quality cases 必须在项目目录内。", str(path)) from exc
    if not path.is_file():
        raise RecipesError("AR701", "recall quality cases 文件不存在。", str(path))
    return path


def _normalize_case(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or not str(raw.get("query") or "").strip():
        raise RecipesError("AR701", "recall quality case 缺 query。", f"index={index}")
    expect_applicable = bool(raw.get("expect_applicable"))
    expected_recipe_id = str(raw.get("expected_recipe_id") or "") or None
    overreach_recipe_id = str(raw.get("overreach_recipe_id") or "") or None
    allow_other_recipe = bool(raw.get("allow_other_recipe"))
    if expect_applicable and not expected_recipe_id:
        raise RecipesError("AR701", "正例缺 expected_recipe_id。", f"index={index}")
    if not expect_applicable and allow_other_recipe and not overreach_recipe_id:
        raise RecipesError("AR701", "允许其他 recipe 的负例缺 overreach_recipe_id。", f"index={index}")
    return {
        "case_id": str(raw.get("case_id") or f"case_{index + 1}"),
        "query": " ".join(str(raw["query"]).split()),
        "expect_applicable": expect_applicable,
        "expected_recipe_id": expected_recipe_id,
        "overreach_recipe_id": overreach_recipe_id,
        "allow_other_recipe": allow_other_recipe,
    }


def _case_kind(case: dict[str, Any]) -> str:
    if case["expect_applicable"]:
        return "positive"
    return "target_overreach" if case["allow_other_recipe"] else "pure_no_match"


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for case in cases:
        key = (
            case["query"],
            case["expect_applicable"],
            case.get("expected_recipe_id"),
            case.get("overreach_recipe_id"),
            case["allow_other_recipe"],
        )
        if key not in unique:
            item = dict(case)
            item["source_case_ids"] = [case["case_id"]]
            unique[key] = item
        else:
            unique[key]["source_case_ids"].append(case["case_id"])
    return list(unique.values())


def _project_recipes(recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "record_id": str(recipe["recipe_id"]),
            "recipe_id": str(recipe["recipe_id"]),
            "target_recipe_id": str(recipe["recipe_id"]),
            "source_kind": "benchmark_recipe_projection",
            "text": recipe_lookup_haystack(recipe),
            "evidence_strength": "benchmark_projection",
        }
        for recipe in recipes
    ]


def _graph_projection(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in corpus:
        recipe_id = item["recipe_id"]
        records.extend(
            [
                {**item, "record_id": f"graph_node:{recipe_id}", "result_type": "node"},
                {
                    **item,
                    "record_id": f"graph_edge:{recipe_id}",
                    "result_type": "edge",
                    "relation_type": "projects_to_recipe",
                    "text": f"benchmark evidence targets recipe {recipe_id} {item['text']}",
                },
            ]
        )
    return records


def _run_core(
    project: Any,
    cases: list[dict[str, Any]],
    recipes: list[dict[str, Any]],
    *,
    priority_rules: list[dict[str, Any]],
    limit: int,
    min_score: int,
) -> dict[str, Any]:
    rows = []
    latencies = []
    for case in cases:
        started = time.perf_counter()
        ranked = rank_recipes_for_lookup(case["query"], recipes, priority_rules=priority_rules)
        top = ranked[0]
        no_match_reason = None
        try:
            lookup = project.lookup(case["query"], strict=True, min_score=min_score)
            selected = str(lookup["recipe"]["recipe_id"])
        except RecipesError as exc:
            if exc.code != "AR242":
                raise
            selected = None
            no_match_reason = exc.problem
        latency = (time.perf_counter() - started) * 1000.0
        latencies.append(latency)
        rows.append(
            _score_case(
                case,
                selected,
                [str(item["recipe_id"]) for item in ranked[:limit]],
                score=float(top["score"]),
                latency_ms=latency,
                no_match_reason=no_match_reason,
            )
        )
    return _backend_report("core", rows, len(recipes), latencies, threshold={"min_score": min_score, "min_match_ratio": 0.5})


def _run_candidate_projection(
    backend: str,
    cases: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    candidate_ranker: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]],
    limit: int,
) -> dict[str, Any]:
    rows = []
    latencies = []
    for case in cases:
        started = time.perf_counter()
        ranked = candidate_ranker(case["query"], records)
        deduped = _dedupe_recipe_results(ranked)
        no_match_reason = recall_no_match_reason(case["query"])
        selected = deduped[0]["recipe_id"] if deduped and not no_match_reason else None
        latency = (time.perf_counter() - started) * 1000.0
        latencies.append(latency)
        rows.append(
            _score_case(
                case,
                selected,
                [item["recipe_id"] for item in deduped[:limit]],
                score=float(deduped[0]["score"]) if deduped else None,
                latency_ms=latency,
                no_match_reason=no_match_reason,
            )
        )
    return _backend_report(
        backend,
        rows,
        len({str(item.get("target_recipe_id") or "") for item in records}),
        latencies,
        threshold={"minimum_lexical_score": 1},
    )


def _run_qwen(
    project: Any,
    cases: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    *,
    corpus_hash: str,
    config: dict[str, Any],
    embedding_caller: Callable[[dict[str, Any], str], list[float]],
    cosine_similarity: Callable[[list[float], list[float]], float],
    limit: int,
    min_score: float,
) -> dict[str, Any]:
    cache_dir = project.recipes_dir / "recall_quality" / "qwen"
    cache_path = cache_dir / f"corpus_{corpus_hash}.json"
    cached = read_json(cache_path, {})
    cache_hit = bool(cached.get("corpus_hash") == corpus_hash and isinstance(cached.get("vectors"), dict))
    vectors: dict[str, list[float]] = cached.get("vectors", {}) if cache_hit else {}
    embedding_latency_ms = 0.0
    if not cache_hit:
        for item in corpus:
            started = time.perf_counter()
            vectors[item["recipe_id"]] = embedding_caller(config, item["text"])
            embedding_latency_ms += (time.perf_counter() - started) * 1000.0
        write_json(
            cache_path,
            {
                "corpus_hash": corpus_hash,
                "model": config.get("model"),
                "dimensions": len(next(iter(vectors.values()))) if vectors else 0,
                "vectors": vectors,
            },
        )
    rows = []
    latencies = []
    for case in cases:
        started = time.perf_counter()
        query_vector = embedding_caller(config, case["query"])
        scored = [
            {"recipe_id": item["recipe_id"], "score": cosine_similarity(query_vector, vectors[item["recipe_id"]])}
            for item in corpus
        ]
        scored.sort(key=lambda item: (item["score"], item["recipe_id"]), reverse=True)
        no_match_reason = recall_no_match_reason(case["query"])
        selected = scored[0]["recipe_id"] if scored and scored[0]["score"] >= min_score and not no_match_reason else None
        latency = (time.perf_counter() - started) * 1000.0
        latencies.append(latency)
        rows.append(
            _score_case(
                case,
                selected,
                [item["recipe_id"] for item in scored[:limit]],
                score=float(scored[0]["score"]) if scored else None,
                latency_ms=latency,
                no_match_reason=no_match_reason,
            )
        )
    report = _backend_report("qwen", rows, len(corpus), latencies, threshold={"cosine_min_score": min_score})
    report["runtime"] = {
        "model": config.get("model"),
        "endpoint": config.get("endpoint"),
        "dimensions": len(next(iter(vectors.values()))) if vectors else 0,
        "corpus_cache_hit": cache_hit,
        "corpus_embedding_latency_ms": round(embedding_latency_ms, 3),
        "cache_path": str(cache_path),
        "cache_bytes": cache_path.stat().st_size if cache_path.exists() else 0,
        "live_query_calls": len(cases),
    }
    return report


def _dedupe_recipe_results(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ranked:
        recipe_id = str(item.get("target_recipe_id") or item.get("recipe_id") or "")
        if not recipe_id or recipe_id in seen:
            continue
        seen.add(recipe_id)
        output.append({"recipe_id": recipe_id, "score": float(item.get("score") or 0.0)})
    return output


def _score_case(
    case: dict[str, Any],
    selected_recipe_id: str | None,
    returned_recipe_ids: list[str],
    *,
    score: float | None,
    latency_ms: float,
    no_match_reason: str | None = None,
) -> dict[str, Any]:
    kind = _case_kind(case)
    expected = case.get("expected_recipe_id")
    overreach = case.get("overreach_recipe_id")
    if kind == "positive":
        passed = selected_recipe_id == expected
        false_recall = selected_recipe_id is not None and selected_recipe_id != expected
    elif kind == "pure_no_match":
        passed = selected_recipe_id is None
        false_recall = selected_recipe_id is not None
    else:
        passed = selected_recipe_id != overreach
        false_recall = selected_recipe_id == overreach
    return {
        "case_id": case["case_id"],
        "case_kind": kind,
        "selected_recipe_id": selected_recipe_id,
        "expected_recipe_id": expected,
        "overreach_recipe_id": overreach,
        "returned_recipe_ids": returned_recipe_ids,
        "top_score": round(score, 6) if score is not None else None,
        "latency_ms": round(latency_ms, 3),
        "no_match_reason": no_match_reason,
        "passed": passed,
        "false_recall": false_recall,
    }


def _backend_report(
    backend: str,
    rows: list[dict[str, Any]],
    corpus_count: int,
    latencies: list[float],
    *,
    threshold: dict[str, Any],
) -> dict[str, Any]:
    positives = [item for item in rows if item["case_kind"] == "positive"]
    pure_negatives = [item for item in rows if item["case_kind"] == "pure_no_match"]
    false_recalls = [item for item in rows if item["false_recall"]]
    top1_accuracy = _ratio(sum(item["passed"] for item in positives), len(positives))
    no_match_accuracy = _ratio(sum(item["passed"] for item in pure_negatives), len(pure_negatives))
    false_recall_rate = _ratio(len(false_recalls), len(rows))
    gate = {
        "passed": top1_accuracy >= 0.8 and no_match_accuracy >= 0.9 and false_recall_rate <= 0.1,
        "top1_accuracy_passed": top1_accuracy >= 0.8,
        "pure_no_match_passed": no_match_accuracy >= 0.9,
        "false_recall_passed": false_recall_rate <= 0.1,
    }
    return {
        "backend": backend,
        "same_corpus": True,
        "projection_only": backend in {"cognee", "graphiti"},
        "corpus_recipe_count": corpus_count,
        "threshold": threshold,
        "metrics": {
            "case_count": len(rows),
            "positive_count": len(positives),
            "top1_correct": sum(item["passed"] for item in positives),
            "top1_accuracy": top1_accuracy,
            "pure_no_match_count": len(pure_negatives),
            "pure_no_match_correct": sum(item["passed"] for item in pure_negatives),
            "pure_no_match_accuracy": no_match_accuracy,
            "false_recall_count": len(false_recalls),
            "false_recall_rate": false_recall_rate,
            "overall_passed": sum(item["passed"] for item in rows),
            "overall_accuracy": _ratio(sum(item["passed"] for item in rows), len(rows)),
            "latency_ms": _latency_summary(latencies),
        },
        "gate": gate,
        "cases": rows,
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {
        "mean": round(sum(ordered) / len(ordered), 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


def _percentile(values: list[float], quantile: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction
