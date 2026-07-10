from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_recipes.lifecycle import active_recipe_ids_for_consumption, recipe_version_rank
from agent_recipes.persistence import RecipesError, make_id, read_json, stable_json, write_json


NEVER_EXPIRES = "9999-12-31T23:59:59Z"


LOOKUP_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "final",
    "for",
    "from",
    "generate",
    "generated",
    "easeoutcubic",
    "in",
    "into",
    "no",
    "of",
    "on",
    "opacity",
    "or",
    "production",
    "ready",
    "scale",
    "the",
    "to",
    "video",
    "with",
    "x_offset",
    "和",
    "给",
    "或",
    "秒",
    "卡片",
    "包装",
    "文案",
    "画面",
    "好看",
    "好看一点",
    "要求",
    "菜谱",
}


LOOKUP_QUERY_PHRASES = [
    "后期包装",
    "完整后期包装流程",
    "大字",
    "花字",
    "字幕",
    "专场",
    "特效",
    "转场",
    "全流程",
    "流程",
    "质量通过",
    "主持人",
    "主持人小窗",
    "小窗",
    "画中画",
    "主播分层",
    "字在人后",
    "人后",
    "抠像",
    "移动人物",
    "三层合成",
    "超大标题",
    "人物遮挡",
    "动态时间线",
    "信息卡",
    "画面太空",
    "证据图",
    "人物手势",
    "手势",
    "首尾图",
    "首尾产品图",
    "过渡视频",
    "视觉目的",
    "运动描述",
    "验收标准",
    "外包模型",
    "另一个场景",
    "语义组",
    "连续口播",
    "固定5秒",
    "原句范围",
    "起止时间",
    "拉伸",
    "关键帧",
    "动画",
    "动效",
    "动效节奏",
    "2秒动效",
    "声音包装",
    "声音",
    "音效",
    "同源声音",
    "同源声音素材",
    "候选规则",
    "候选经验",
    "剪辑课程",
    "课程",
    "快进快出",
    "慢放",
    "future_motion_test",
    "先看文案",
    "补场景",
    "补情绪",
    "补节奏",
    "没有工作就删",
    "关键词",
    "动作",
    "切点",
    "情绪转折",
    "人耳审核",
    "真实输出",
    "课程音频",
    "前3秒",
    "前 3 秒",
    "利益点",
    "纯色占位",
    "示范音频",
    "利益点视觉化",
    "爆点卡片",
    "本地爆点卡片",
    "脚本文字锁定",
    "缺失画面支撑",
    "画面支撑卡片",
    "本地画面支撑卡片",
    "重新渲染复检",
    "课程笔记",
    "产物",
    "掌握",
    "正式用",
    "后期包装技能",
    "新版",
    "旧版",
    "比旧版",
    "高级",
    "通过",
    "最窄安全范围",
    "逐帧剪辑表",
    "冒充逐帧剪辑表",
    "字幕压到手",
    "绿色小标签",
    "网页按钮",
    "白色字",
    "浮着",
    "title + title + title",
]


LOOKUP_TERM_ALIASES = {
    "后期包装": ["postprod", "post-production", "post production", "packaging"],
    "完整后期包装流程": ["complete post-production workflow", "complete post production workflow", "full post-production workflow"],
    "大字": ["large words", "big text", "large typography", "typography"],
    "花字": ["decorative text", "styled text", "typography"],
    "字幕": ["subtitle", "subtitles", "caption", "captions"],
    "专场": ["special segment", "scene package", "segment"],
    "特效": ["effect", "effects", "vfx"],
    "转场": ["transition", "transitions"],
    "全流程": ["full workflow", "complete workflow"],
    "流程": ["workflow", "process"],
    "质量通过": ["quality pass", "quality passed"],
    "主播": ["presenter", "host"],
    "主持人": ["presenter", "host"],
    "主持人小窗": ["pip", "host placement", "presenter mode"],
    "小窗": ["pip", "host placement"],
    "画中画": ["pip", "host placement", "presenter mode"],
    "主播分层": ["presenter", "host", "layer"],
    "字在人后": ["behind presenter", "behind the presenter", "behind host"],
    "人后": ["behind presenter", "behind the presenter", "behind host"],
    "抠像": ["cutout", "matte", "keying"],
    "移动人物": ["moving presenter", "moving host", "moving video"],
    "三层合成": ["three layer", "layer depth", "layer"],
    "超大标题": ["large words", "big text", "large typography"],
    "人物遮挡": ["behind presenter", "presenter foreground", "occludes"],
    "动态时间线": ["moving video", "real timeline", "timeline"],
    "信息卡": ["information card", "infocard", "callout", "card"],
    "画面太空": ["callout", "infocard", "semantic purpose", "videos need packaging"],
    "证据图": ["proof detail", "evidence visual"],
    "人物手势": ["presenter", "action"],
    "手势": ["action", "presenter action"],
    "首尾图": ["first frame", "last frame", "keyframe"],
    "首尾产品图": ["first frame", "last frame", "keyframe"],
    "过渡视频": ["transition", "true video", "real video"],
    "视觉目的": ["visual purpose", "visual_purpose"],
    "运动描述": ["video motion prompt", "video_motion_prompt", "motion"],
    "验收标准": ["verification", "visual check", "acceptance"],
    "外包模型": ["provider", "model"],
    "另一个场景": ["unrelated scene", "new unrelated scene"],
    "语义组": ["semantic group", "semantic visual group"],
    "连续口播": ["continuous sentences", "talking-head video"],
    "固定5秒": ["fixed-length clips", "fixed length clips"],
    "原句范围": ["source sentence range"],
    "起止时间": ["start/end time", "start time", "end time"],
    "拉伸": ["force them into the spoken script", "target duration"],
    "关键帧": ["keyframe", "keyframes"],
    "动画": ["animation", "motion"],
    "动效": ["motion", "animation"],
    "动效节奏": ["motion", "animation", "timing", "rhythm", "pacing"],
    "2秒动效": ["2 second motion", "2s motion", "two second motion"],
    "声音包装": ["sound packaging", "bgm", "sfx", "音效"],
    "声音": ["sound", "bgm", "sfx", "audio"],
    "音效": ["sfx", "sound effect", "sound"],
    "同源声音": ["source sound", "same source sound", "sound"],
    "同源声音素材": ["source sound", "same source sound", "sound"],
    "候选规则": ["candidate gate", "candidate rule", "candidate"],
    "候选经验": ["candidate experience", "candidate"],
    "剪辑课程": ["learning", "course", "learning material"],
    "课程": ["learning", "course", "learning material"],
    "前3秒": ["first 3 seconds", "hook"],
    "前 3 秒": ["first 3 seconds", "hook"],
    "利益点": ["benefit", "hook_gate_benefit_not_visualized"],
    "纯色占位": ["missing visual support", "self_media_missing_visual_support"],
    "示范音频": ["course audio", "课程音频"],
    "利益点视觉化": ["benefit visualized", "hook_gate_benefit_not_visualized"],
    "爆点卡片": ["hook benefit card", "use_hook_benefit_card"],
    "本地爆点卡片": ["local hook benefit card", "use_hook_benefit_card"],
    "脚本文字锁定": ["script text locked", "脚本文字保持锁定"],
    "缺失画面支撑": ["missing visual support", "self_media_missing_visual_support"],
    "画面支撑卡片": ["visual support card", "self_media_missing_visual_support"],
    "本地画面支撑卡片": ["local visual support card", "self_media_missing_visual_support"],
    "重新渲染复检": ["rerender review", "重新渲染复检"],
    "课程笔记": ["course evidence", "course learning", "source/reference"],
    "产物": ["source/reference", "reference material", "production_intermediate"],
    "掌握": ["proof of skill", "learned", "skill"],
    "正式用": ["official", "use-now", "official/use-now"],
    "后期包装技能": ["proof of skill", "course learning", "official/use-now"],
    "新版": ["before after", "before/after", "new version"],
    "旧版": ["before after", "before/after", "old version"],
    "比旧版": ["before after", "before/after"],
    "高级": ["review", "pass_questions"],
    "通过": ["pass_questions", "pass", "passed"],
    "快进快出": ["measurable choices", "duration", "rhythm", "timing"],
    "慢放": ["duration", "rhythm", "timing", "measurable choices"],
    "future_motion_test": ["keyframe motion reject", "motion reject", "keyframe reject"],
    "字幕压到手": ["subtitle overlaps hands", "subtitle overlaps hands or face"],
    "绿色小标签": ["green tag", "tag color", "green tag reads as a button"],
    "网页按钮": ["web button", "website button", "button", "tag reads as a clickable button"],
    "白色字": ["white word", "white word floats"],
    "浮着": ["floats", "no shadow", "no shadow/contrast"],
    "title + title + title": ["title + title + title", "no secondary role"],
}


def _unique_text(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _normalize_problem(text: str) -> str:
    return " ".join(text.casefold().split())


def lookup_query_terms(query: str) -> list[str]:
    lowered = query.casefold()
    split_terms = [
        term
        for term in re.findall(r"[a-z0-9_./+-]+|[\u4e00-\u9fff]+", lowered)
        if lookup_split_term_is_useful(term) and not lookup_query_term_is_negated(lowered, term)
    ]
    phrase_terms = [
        phrase.casefold()
        for phrase in LOOKUP_QUERY_PHRASES
        if phrase.casefold() in lowered and not lookup_query_term_is_negated(lowered, phrase.casefold())
    ]
    return _unique_text(
        [token for token in [*split_terms, *phrase_terms] if lookup_term_is_searchable(token)]
    )


def lookup_split_term_is_useful(term: str) -> bool:
    token = term.strip()
    if not token or re.fullmatch(r"[./+-]+", token):
        return False
    phrases = {phrase.casefold() for phrase in LOOKUP_QUERY_PHRASES}
    if re.fullmatch(r"[\u4e00-\u9fff]+", token) and token.casefold() not in phrases:
        return False
    return True


def lookup_query_term_is_negated(query: str, term: str) -> bool:
    token = term.strip().casefold()
    if not token:
        return False
    positions = [match.start() for match in re.finditer(re.escape(token), query)]
    if not positions:
        return False
    markers = [
        "不涉及",
        "不需要",
        "不要",
        "不用",
        "不做",
        "没有",
        "无",
        "会不会套",
        "是否套用",
        "是否直接套",
        "能不能直接套",
        "不能硬套",
        "不硬套",
    ]
    for pos in positions:
        before = query[max(0, pos - 24):pos]
        if not any(marker in before for marker in markers):
            return False
    return True


def lookup_term_is_searchable(term: str) -> bool:
    token = term.strip().casefold()
    if not token or token in LOOKUP_STOPWORDS:
        return False
    return not re.fullmatch(r"\d+(?:\.\d+)?", token)


def lookup_query_is_overbroad_single_recipe_request(query: str) -> bool:
    lowered = query.casefold()
    component_terms = [
        "大字", "花字", "字幕", "专场", "特效", "转场", "声音",
        "big text", "subtitle", "captions", "transition", "sound", "sfx",
    ]
    broad_markers = [
        "完整后期包装", "全流程", "完整流程", "一次性保证", "一次性质量通过",
        "完整后期特效转场库", "complete workflow", "full workflow", "complete postprod",
        "complete post-production",
    ]
    quality_markers = [
        "final quality", "quality passed", "release ready", "public release", "production ready",
        "发布", "最终视频", "导出", "质量通过",
    ]
    component_count = sum(1 for term in component_terms if term in lowered)
    if component_count >= 3 and any(marker in lowered for marker in broad_markers):
        return True
    if any(marker in lowered for marker in broad_markers) and any(marker in lowered for marker in quality_markers):
        return True
    if "provider" in lowered and any(marker in lowered for marker in quality_markers):
        return True
    return "complete workflow" in lowered and any(marker in lowered for marker in quality_markers)


def recall_no_match_reason(query: str) -> str | None:
    if lookup_query_is_overbroad_single_recipe_request(query):
        return "任务范围太宽，不能推荐单条 recipe；先拆成局部任务分别 lookup/lock。"
    return None


def lookup_query_is_subtitle_ocr_asr_repair(query: str) -> bool:
    lowered = query.casefold()
    has_subtitle = any(marker in lowered for marker in ["字幕", "subtitle", "caption"])
    return has_subtitle and any(
        marker in lowered for marker in ["ocr", "asr", "识别失败", "智能字幕", "导入字幕", "文本校对"]
    )


def recipe_declares_subtitle_ocr_asr_scope(recipe: dict[str, Any]) -> bool:
    scope_text = stable_json(
        {
            "recipe_id": recipe.get("recipe_id"),
            "title": recipe.get("title"),
            "scope": recipe.get("scope"),
            "use_when": recipe.get("use_when"),
        }
    ).casefold()
    has_subtitle = any(marker in scope_text for marker in ["字幕", "subtitle", "caption"])
    has_ocr_asr = any(marker in scope_text for marker in ["ocr", "asr", "智能字幕", "识别失败"])
    return has_subtitle and has_ocr_asr


def load_lookup_priority_rules(recipes_dir: Path) -> list[dict[str, Any]]:
    path = recipes_dir / "lookup_priority_rules.json"
    if not path.exists():
        return []
    doc = read_json(path, {})
    raw_rules = doc.get("rules") if isinstance(doc, dict) else doc
    if not isinstance(raw_rules, list):
        return []
    rules: list[dict[str, Any]] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict) or raw_rule.get("enabled") is False:
            continue
        if not str(raw_rule.get("preferred_recipe_id") or "").strip():
            continue
        rules.append(raw_rule)
    return rules


def lookup_priority_rule_matches_query(query: str, rule: dict[str, Any]) -> tuple[bool, list[str]]:
    query_blob = query.casefold()
    normalized_query = _normalize_problem(query)
    any_terms = [str(term).strip() for term in rule.get("when_query_contains_any", []) if str(term).strip()]
    all_terms = [str(term).strip() for term in rule.get("when_query_contains_all", []) if str(term).strip()]
    matched_any = [term for term in any_terms if lookup_priority_term_in_query(term, query_blob, normalized_query)]
    matched_all = [term for term in all_terms if lookup_priority_term_in_query(term, query_blob, normalized_query)]
    if any_terms and not matched_any:
        return False, []
    if all_terms and len(matched_all) != len(all_terms):
        return False, []
    if not any_terms and not all_terms:
        return False, []
    return True, _unique_text(matched_any + matched_all)


def lookup_priority_term_in_query(term: str, query_blob: str, normalized_query: str) -> bool:
    raw_term = term.casefold()
    normalized_term = _normalize_problem(term)
    raw_match = bool(raw_term and raw_term in query_blob and not lookup_query_term_is_negated(query_blob, raw_term))
    normalized_match = bool(
        normalized_term
        and normalized_term in normalized_query
        and not lookup_query_term_is_negated(normalized_query, normalized_term)
    )
    return raw_match or normalized_match


def lookup_priority_bonus_for_recipe(
    *,
    recipe_id: str,
    query: str,
    base_score: int,
    priority_rules: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    total_bonus = 0
    applied: list[dict[str, Any]] = []
    for rule in priority_rules:
        if str(rule.get("preferred_recipe_id") or "") != recipe_id:
            continue
        try:
            min_base_score = int(rule.get("min_base_score", 1))
        except (TypeError, ValueError):
            min_base_score = 1
        if base_score < min_base_score:
            continue
        matched, trigger_terms = lookup_priority_rule_matches_query(query, rule)
        if not matched:
            continue
        try:
            bonus = int(rule.get("bonus", 100))
        except (TypeError, ValueError):
            bonus = 100
        if bonus <= 0:
            continue
        total_bonus += bonus
        applied.append(
            {
                "rule_id": str(rule.get("rule_id") or make_id("lookup_priority_rule", recipe_id, trigger_terms, bonus)),
                "bonus": bonus,
                "trigger_terms": trigger_terms,
                "preferred_recipe_id": recipe_id,
                "fallback_recipe_id": rule.get("fallback_recipe_id"),
                "reason": rule.get("reason"),
            }
        )
    return total_bonus, applied


def rank_recipes_for_lookup(
    query: str,
    recipes: list[dict[str, Any]],
    *,
    priority_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    terms = lookup_query_terms(query)
    q = query.casefold()
    rules = priority_rules or []
    ranked: list[dict[str, Any]] = []
    for recipe in recipes:
        haystack = recipe_lookup_haystack(recipe).casefold()
        matched_terms = [
            term
            for term in terms
            if any(alternative in haystack for alternative in lookup_term_alternatives(term))
        ]
        score = len(matched_terms)
        if score == 0 and q:
            score = 1 if q in haystack else 0
        recipe_id = str(recipe.get("recipe_id", ""))
        priority_bonus, applied_rules = lookup_priority_bonus_for_recipe(
            recipe_id=recipe_id,
            query=query,
            base_score=score,
            priority_rules=rules,
        )
        ranked.append(
            {
                "score": score + priority_bonus,
                "base_score": score,
                "priority_bonus": priority_bonus,
                "priority_rules_applied": applied_rules,
                "matched_terms": matched_terms,
                "query_terms": terms,
                "version_rank": recipe_version_rank(recipe),
                "recipe_id": recipe_id,
                "recipe": recipe,
            }
        )
    ranked.sort(
        key=lambda item: (item["score"], item["base_score"], item["version_rank"], item["recipe_id"]),
        reverse=True,
    )
    return ranked


def recipe_lookup_haystack(recipe: dict[str, Any]) -> str:
    positive_keys = [
        "recipe_id", "title", "scope", "use_when", "inputs_required", "steps", "checklist_item",
        "verified_path", "visual_check", "good_example", "success_means", "outputs_expected",
        "verification", "source_truth_to_read",
    ]
    payload = {key: recipe.get(key) for key in positive_keys if key in recipe}
    if recipe_is_lookup_guardrail(recipe):
        guardrail_keys = ["forbidden_path", "cannot_claim", "failure_signal", "failure_signals", "do_not_use_when"]
        payload.update({key: recipe.get(key) for key in guardrail_keys if key in recipe})
    return stable_json(payload)


def recipe_is_lookup_guardrail(recipe: dict[str, Any]) -> bool:
    text = stable_json(
        {
            "recipe_id": recipe.get("recipe_id"),
            "title": recipe.get("title"),
            "scope": recipe.get("scope"),
            "use_when": recipe.get("use_when"),
        }
    ).casefold()
    markers = ["guardrail", "boundary", "reject gate", "reject_gate", "motion reject", "failure gate", "护栏", "边界"]
    return any(marker in text for marker in markers)


def lookup_term_alternatives(term: str) -> list[str]:
    lowered = term.casefold()
    aliases = LOOKUP_TERM_ALIASES.get(term) or LOOKUP_TERM_ALIASES.get(lowered) or []
    return _unique_text([lowered, *(alias.casefold() for alias in aliases)])


def lookup_applicability(ranked_item: dict[str, Any], *, min_score: int) -> dict[str, Any]:
    score = int(ranked_item.get("score") or 0)
    base_score = int(ranked_item.get("base_score", score) or 0)
    priority_bonus = int(ranked_item.get("priority_bonus") or 0)
    query_terms = ranked_item.get("query_terms", []) or []
    match_ratio = base_score / len(query_terms) if query_terms else 0.0
    min_match_ratio = 0.5
    status = "strong" if score >= min_score and match_ratio >= min_match_ratio else "weak"
    return {
        "status": status,
        "score": score,
        "base_score": base_score,
        "priority_bonus": priority_bonus,
        "priority_rules_applied": ranked_item.get("priority_rules_applied", []),
        "min_score": min_score,
        "match_ratio": round(match_ratio, 3),
        "min_match_ratio": min_match_ratio,
        "matched_terms": ranked_item.get("matched_terms", []),
        "missing_query_terms": [
            term for term in query_terms if term not in set(ranked_item.get("matched_terms", []))
        ],
        "cannot_claim": [
            "不能说 lookup 命中等于 recipe 适用于任务。",
            "weak 匹配不能直接 lock；需要 strict lookup 或 lock --query 通过。",
        ],
    }


def lookup_execution_policy(
    query: str,
    all_recipes: list[dict[str, Any]],
    *,
    retired_recipe_ids: set[str],
    recipes_dir: Path,
    strict: bool = False,
    min_score: int = 2,
) -> dict[str, Any]:
    if min_score < 1:
        raise RecipesError("AR243", "lookup min-score 必须大于 0。", f"收到：{min_score}", "传入正整数。")
    if not all_recipes:
        raise RecipesError("AR240", "没有正式 recipe。", "recipes/ 目录为空。", "先运行 compile + review --accept。")
    active_ids, inactive_ids = active_recipe_ids_for_consumption(
        all_recipes,
        retired_recipe_ids=retired_recipe_ids,
    )
    active_set = set(active_ids)
    recipes = [recipe for recipe in all_recipes if str(recipe.get("recipe_id") or "") in active_set]
    if not recipes:
        raise RecipesError(
            "AR244",
            "没有可消费的 active recipe。",
            f"inactive_recipe_ids={inactive_ids}",
            "创建新的 candidate 并通过人工 review；不要恢复已退役 recipe id。",
        )
    if strict and lookup_query_is_overbroad_single_recipe_request(query):
        raise RecipesError(
            "AR242",
            "任务范围太宽，不能用单条 recipe 承接。",
            f"query={query}",
            "先拆成局部任务分别 lookup/lock；全流程或发布质量必须另走人工 review/live task 验收。",
        )
    ranked = rank_recipes_for_lookup(
        query,
        recipes,
        priority_rules=load_lookup_priority_rules(recipes_dir),
    )
    selected = ranked[0]["recipe"]
    if strict and lookup_query_is_subtitle_ocr_asr_repair(query) and not recipe_declares_subtitle_ocr_asr_scope(selected):
        raise RecipesError(
            "AR242",
            "字幕 OCR/ASR 修复没有足够适用的 recipe。",
            f"query={query}; top_recipe={selected.get('recipe_id')}",
            "补充专门的字幕 OCR/ASR 修复菜谱，或作为 no-match 进入 review。",
        )
    applicability = lookup_applicability(ranked[0], min_score=min_score)
    if strict and applicability["status"] != "strong":
        raise RecipesError(
            "AR242",
            "没有足够适用的 recipe。",
            f"query={query}; top_recipe={selected.get('recipe_id')}; score={applicability['score']}; min_score={min_score}",
            "补充更具体的菜谱，或用 lookup-pressure/人工 review 判断后再 lock。",
        )
    return {
        "recipe": selected,
        "applicability": applicability,
        "inactive_recipe_ids": inactive_ids,
        "candidates": [
            {
                "recipe_id": item["recipe"].get("recipe_id"),
                "score": item["score"],
                "base_score": item.get("base_score", item["score"]),
                "priority_bonus": item.get("priority_bonus", 0),
                "priority_rules_applied": item.get("priority_rules_applied", []),
                "matched_terms": item["matched_terms"],
                "applicability": lookup_applicability(item, min_score=min_score)["status"],
            }
            for item in ranked[:5]
        ],
    }


def execution_lock_id(recipe: dict[str, Any], *, task: str, session_id: str) -> str:
    return make_id("lock", recipe["recipe_id"], recipe["recipe_hash"], task, session_id)


def build_execution_lock(
    recipe: dict[str, Any],
    *,
    lock_id: str,
    task: str,
    session_id: str,
    outcome_quality: dict[str, Any],
    owner_agent: str = "codex",
    query: str | None = None,
    applicability: dict[str, Any] | None = None,
    expires_at: str = NEVER_EXPIRES,
) -> dict[str, Any]:
    recommendation = str(outcome_quality.get("execution_recommendation") or "normal")
    claim_limits = list(recipe.get("cannot_claim", []))
    stop_lines = [recipe["stop_line"]] if recipe.get("stop_line") else []
    if recommendation in {"caution", "degraded"}:
        claim_limits.append("该 recipe 有新失败结果；本次输出必须人工复核，不能直接 claim 质量通过。")
    if recommendation == "degraded":
        stop_lines.append("该 recipe 已自动降级；执行前先确认失败模式仍不适用于当前任务。")
    lock = {
        "lock_id": lock_id,
        "task": task,
        "owner_agent": owner_agent,
        "session_id": session_id,
        "recipe_ids": [recipe["recipe_id"]],
        "recipe_versions": [recipe["version"]],
        "recipe_hashes": [recipe["recipe_hash"]],
        "allowed_actions": ["execute_recipe", "capture_success", "capture_failure", "capture_unknown"],
        "forbidden_actions": recipe.get("forbidden_path", []),
        "claim_limits": claim_limits,
        "stop_lines": stop_lines,
        "verification_required": recipe.get("verification", []),
        "outcome_quality": outcome_quality,
        "expires_at": expires_at,
        "status": "active",
    }
    if query:
        lock["lookup_query"] = query
        lock["applicability"] = applicability or {}
    return lock


def validate_execution_lock(
    recipes_dir: Path,
    lock_id: str,
    *,
    retired_recipe_ids: set[str],
    load_recipe: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    lock_path = recipes_dir / "locks" / f"{lock_id}.json"
    if not lock_path.exists():
        raise RecipesError("AR410", "active lock 不存在。", f"找不到 lock：{lock_id}", "重新运行 lookup + lock。")
    lock = read_json(lock_path, {})
    if lock.get("status") != "active":
        raise RecipesError("AR412", "lock 不是 active。", f"lock 状态：{lock.get('status')}", "重新创建 lock。")
    expires_at = lock.get("expires_at")
    if expires_at and expires_at != NEVER_EXPIRES:
        try:
            dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("expires_at 必须包含时区。")
            if dt < datetime.now(timezone.utc):
                raise RecipesError("AR413", "lock 已过期。", f"expires_at={expires_at}", "重新运行 lookup + lock。")
        except ValueError as exc:
            raise RecipesError("AR414", "lock expires_at 格式错误。", str(exc), "修复或重建 lock。") from exc
    for recipe_id, expected_hash in zip(lock.get("recipe_ids", []), lock.get("recipe_hashes", [])):
        if recipe_id in retired_recipe_ids:
            raise RecipesError(
                "AR432",
                "lock 引用的 recipe 已退役，停止写入。",
                f"lock_id={lock_id}, recipe_id={recipe_id}",
                "重新 lookup active recipe 并创建新 lock。",
            )
        recipe = load_recipe(str(recipe_id))
        if recipe.get("recipe_hash") != expected_hash:
            raise RecipesError(
                "AR411",
                "recipe hash 与 lock 不一致，停止写入。",
                f"{recipe_id}: lock={expected_hash}, current={recipe.get('recipe_hash')}",
                "重新运行 lookup + lock。",
            )
    return lock


def retire_active_execution_locks(
    recipes_dir: Path,
    recipe_id: str,
    *,
    status: str,
    retired_at: str,
    retired_by_event_id: str,
) -> list[str]:
    retired: list[str] = []
    for lock_path in (recipes_dir / "locks").glob("*.json"):
        lock = read_json(lock_path, {})
        if lock.get("status") != "active" or recipe_id not in lock.get("recipe_ids", []):
            continue
        lock["status"] = status
        lock["retired_at"] = retired_at
        lock["retired_by_event_id"] = retired_by_event_id
        lock["retired_recipe_id"] = recipe_id
        write_json(lock_path, lock)
        retired.append(str(lock_path))
    return retired


def retire_stale_execution_locks(
    recipes_dir: Path,
    recipe_id: str,
    recipe_hash: str,
    *,
    superseded_at: str,
    superseded_by_event_id: str,
) -> list[str]:
    retired: list[str] = []
    for lock_path in (recipes_dir / "locks").glob("*.json"):
        lock = read_json(lock_path, {})
        if lock.get("status") != "active":
            continue
        pairs = zip(lock.get("recipe_ids", []), lock.get("recipe_hashes", []))
        stale = any(locked_id == recipe_id and locked_hash != recipe_hash for locked_id, locked_hash in pairs)
        if not stale:
            continue
        lock["status"] = "superseded"
        lock["superseded_at"] = superseded_at
        lock["superseded_by_event_id"] = superseded_by_event_id
        lock["superseded_recipe_id"] = recipe_id
        lock["superseded_recipe_hash"] = recipe_hash
        write_json(lock_path, lock)
        retired.append(str(lock_path))
    return retired
