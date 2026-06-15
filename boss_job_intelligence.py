#!/usr/bin/env python3
"""
岗位运营层：评分、去重、状态建议、profile 与数据自检。

这里借鉴 career-ops 的思路，把“岗位是否值得投递”从浏览器点击流程里拆出来。
目标是先用本地规则做稳定判断，避免无效岗位进入待投递池。
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse


DEFAULT_PROFILE = {
    "target_roles": ["Python", "AI", "大模型", "RAG", "Agent", "自动化", "后端", "运维开发", "全栈"],
    "skills": [
        "Python",
        "Linux",
        "Flask",
        "FastAPI",
        "Node.js",
        "TypeScript",
        "JavaScript",
        "RAG",
        "Agent",
        "LangChain",
        "MCP",
        "Docker",
        "MySQL",
        "PostgreSQL",
    ],
    "preferred_cities": ["深圳", "杭州", "广州", "成都", "武汉", "上海", "北京"],
    "avoid_keywords": ["专家", "经理", "商务", "销售", "客服", "讲师", "培训", "主播", "运营"],
    "hard_block_keywords": ["销售", "客服", "主播", "兼职", "实习", "培训讲师"],
    "min_salary_k": 8,
    "min_fit_score": 60,
}

ALLOWED_APPLICATION_STATUSES = {"pending", "applied", "replied", "skipped", "failed", "missing_url"}


def _split_terms(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        values = re.split(r"[,，、\n\r]+", str(raw))
    return [str(v).strip() for v in values if str(v).strip()]


def _lower_terms(raw: Any) -> list[str]:
    return [t.lower() for t in _split_terms(raw)]


def _job_text(job: dict) -> str:
    fields = ("title", "job_title", "company", "salary", "city", "experience", "education", "description", "hr_name", "hr_title")
    return " ".join(str(job.get(k, "") or "") for k in fields).lower()


def _job_title(job: dict) -> str:
    return str(job.get("job_title") or job.get("title") or "").strip()


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return urljoin("https://www.zhipin.com", url)


def make_dedup_key(job: dict) -> str:
    """稳定去重键：优先岗位 URL；缺 URL 时用岗位核心字段哈希。"""
    url = _normalize_url(job.get("job_url") or job.get("url") or "")
    if url:
        parsed = urlparse(url)
        return f"url:{parsed.netloc}{parsed.path}".lower()
    raw = "|".join(
        str(job.get(k, "") or "").strip().lower()
        for k in ("job_title", "title", "company", "city", "salary")
    )
    return "job:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def salary_bounds_k(text: str) -> Optional[tuple[float, float]]:
    """解析 BOSS 常见薪资到月薪 K。日薪/时薪不参与自动评分。"""
    if not text:
        return None
    decoded = "".join(str(ord(c) - 0xE030) if 0xE030 <= ord(c) <= 0xE039 else c for c in str(text))
    if any(unit in decoded for unit in ("元/天", "/天", "元/时", "/时")):
        return None
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", decoded)]
    if not nums:
        return None
    low = nums[0]
    high = nums[1] if len(nums) > 1 else low
    upper = decoded.upper()
    if "万" in decoded and "K" not in upper:
        low *= 10
        high *= 10
    return low, high


def load_candidate_profile(settings: dict) -> tuple[dict, list[str]]:
    """从设置表加载 profile。返回 profile 与配置问题列表。"""
    profile = dict(DEFAULT_PROFILE)
    issues: list[str] = []

    raw = settings.get("candidate_profile_json", "")
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    if value not in (None, ""):
                        profile[key] = value
            else:
                issues.append("candidate_profile_json 不是 JSON 对象")
        except json.JSONDecodeError as e:
            issues.append(f"candidate_profile_json 格式错误: {e}")

    search_keywords = _split_terms(settings.get("search_keywords"))
    if search_keywords:
        existing = set(_split_terms(profile.get("target_roles")))
        profile["target_roles"] = list(existing.union(search_keywords))

    default_city = (settings.get("default_city") or "").strip()
    if default_city and default_city != "全国":
        cities = _split_terms(profile.get("preferred_cities"))
        if default_city not in cities:
            profile["preferred_cities"] = [default_city] + cities

    try:
        profile["min_salary_k"] = float(profile.get("min_salary_k") or settings.get("min_salary_k") or 8)
    except (TypeError, ValueError):
        profile["min_salary_k"] = 8
        issues.append("min_salary_k 无效，已按 8K 处理")

    try:
        profile["min_fit_score"] = int(float(settings.get("min_fit_score") or profile.get("min_fit_score") or 60))
    except (TypeError, ValueError):
        profile["min_fit_score"] = 60
        issues.append("min_fit_score 无效，已按 60 处理")

    return profile, issues


def score_job(
    job: dict,
    profile: dict,
    selected_city: str = "",
    salary_min: Optional[float] = None,
    exclude_keywords: str = "",
    required_keywords: Optional[list[str]] = None,
) -> dict:
    """返回 0-100 分、等级、投递建议和可读原因。"""
    text = _job_text(job)
    title = _job_title(job).lower()
    selected_city = (selected_city or "").strip()
    min_salary = float(salary_min if salary_min is not None else profile.get("min_salary_k", 8) or 8)
    min_score = int(profile.get("min_fit_score", 60) or 60)

    target_roles = _lower_terms(profile.get("target_roles"))
    skills = _lower_terms(profile.get("skills"))
    preferred_cities = _split_terms(profile.get("preferred_cities"))
    avoid_terms = _lower_terms(profile.get("avoid_keywords")) + _lower_terms(exclude_keywords)
    hard_terms = _lower_terms(profile.get("hard_block_keywords"))
    required = [str(k).strip().lower() for k in (required_keywords or []) if str(k).strip()]

    score = 45
    reasons: list[str] = []
    risks: list[str] = []

    target_hits = [t for t in target_roles if t and t in title]
    skill_hits = [s for s in skills if s and s in text]
    avoid_hits = sorted({t for t in avoid_terms if t and t in text})
    hard_hits = sorted({t for t in hard_terms if t and t in text})

    if target_hits:
        add = min(24, 10 + len(target_hits) * 4)
        score += add
        reasons.append("岗位标题命中: " + "、".join(target_hits[:4]))
    else:
        score -= 14
        risks.append("岗位标题未命中目标方向")

    if required and not any(k in title for k in required):
        score -= 18
        risks.append("标题未命中本次搜索关键词")

    if skill_hits:
        add = min(22, len(skill_hits) * 4)
        score += add
        reasons.append("技能匹配: " + "、".join(skill_hits[:6]))
    else:
        score -= 8
        risks.append("未识别到明显技能匹配")

    salary = salary_bounds_k(str(job.get("salary", "") or ""))
    if salary:
        low, high = salary
        if low >= min_salary:
            score += 16
            reasons.append(f"薪资下限 {low:g}K 达标")
        elif high >= min_salary:
            score += 5
            risks.append(f"薪资下限 {low:g}K 偏低")
        else:
            score -= 28
            risks.append(f"薪资 {low:g}-{high:g}K 低于 {min_salary:g}K")
    else:
        score -= 6
        risks.append("薪资无法解析")

    city_text = str(job.get("city", "") or "")
    if selected_city and selected_city != "全国":
        if selected_city in city_text:
            score += 10
            reasons.append("城市符合筛选")
        elif city_text:
            score -= 14
            risks.append("城市可能不符合筛选")
    elif city_text and any(c and c in city_text for c in preferred_cities):
        score += 8
        reasons.append("城市在偏好范围")

    if avoid_hits:
        score -= min(34, len(avoid_hits) * 10)
        risks.append("命中避开词: " + "、".join(avoid_hits[:5]))
    if hard_hits:
        score -= 60
        risks.append("命中硬屏蔽词: " + "、".join(hard_hits[:5]))

    score = max(0, min(100, int(round(score))))
    if score >= 85:
        level = "A"
    elif score >= 75:
        level = "B"
    elif score >= 65:
        level = "C"
    elif score >= 50:
        level = "D"
    else:
        level = "F"

    recommendation = "apply" if score >= min_score and not hard_hits else "skip"
    if recommendation == "apply" and risks:
        recommendation = "review"

    reason_parts = (risks + reasons) if recommendation == "skip" else (reasons + risks)
    reason = "；".join(reason_parts[:4]) or "信息不足，建议人工确认"
    return {
        "fit_score": score,
        "fit_level": level,
        "fit_recommendation": recommendation,
        "fit_reason": reason,
        "fit_detail": {
            "matched_targets": target_hits[:8],
            "matched_skills": skill_hits[:12],
            "avoid_hits": avoid_hits[:8],
            "hard_hits": hard_hits[:8],
            "risks": risks,
            "reasons": reasons,
            "min_score": min_score,
            "scored_at": datetime.now().isoformat(timespec="seconds"),
        },
    }


def enrich_job(
    job: dict,
    profile: dict,
    selected_city: str = "",
    salary_min: Optional[float] = None,
    exclude_keywords: str = "",
    required_keywords: Optional[list[str]] = None,
) -> dict:
    item = dict(job)
    item["dedup_key"] = make_dedup_key(item)
    scored = score_job(item, profile, selected_city, salary_min, exclude_keywords, required_keywords)
    item.update(scored)
    item["fit_detail"] = json.dumps(scored["fit_detail"], ensure_ascii=False)
    if scored["fit_recommendation"] == "skip":
        item["status"] = "skipped"
    elif not item.get("status"):
        item["status"] = "pending"
    return item


def run_data_doctor(get_db: Callable[[], Any], settings: dict) -> dict:
    """数据一致性检查，供 /api/doctor 使用。"""
    db = get_db()
    profile, profile_issues = load_candidate_profile(settings)
    min_score = int(profile.get("min_fit_score", 60) or 60)

    duplicate_urls = db.execute(
        """SELECT job_url, COUNT(*) AS cnt FROM applications
           WHERE job_url IS NOT NULL AND job_url!=''
           GROUP BY job_url HAVING cnt > 1 LIMIT 20"""
    ).fetchall()
    duplicate_dedup = db.execute(
        """SELECT dedup_key, COUNT(*) AS cnt FROM applications
           WHERE dedup_key IS NOT NULL AND dedup_key!=''
           GROUP BY dedup_key HAVING cnt > 1 LIMIT 20"""
    ).fetchall()
    invalid_status = db.execute(
        "SELECT id, status FROM applications WHERE status NOT IN ({}) LIMIT 20".format(
            ",".join("?" for _ in ALLOWED_APPLICATION_STATUSES)
        ),
        tuple(ALLOWED_APPLICATION_STATUSES),
    ).fetchall()
    low_score_pending = db.execute(
        """SELECT COUNT(*) AS cnt FROM applications
           WHERE status='pending' AND fit_score IS NOT NULL AND fit_score > 0 AND fit_score < ?""",
        (min_score,),
    ).fetchone()
    missing_url_pending = db.execute(
        "SELECT COUNT(*) AS cnt FROM applications WHERE status='pending' AND (job_url IS NULL OR job_url='')"
    ).fetchone()
    stale_pending = db.execute(
        "SELECT COUNT(*) AS cnt FROM applications WHERE status='pending' AND datetime(updated_at) < datetime('now','localtime','-7 days')"
    ).fetchone()

    checks = {
        "profile": {
            "ok": not profile_issues,
            "detail": "正常" if not profile_issues else "；".join(profile_issues),
        },
        "duplicate_urls": {
            "ok": len(duplicate_urls) == 0,
            "detail": f"{len(duplicate_urls)} 组重复 URL",
        },
        "duplicate_dedup_keys": {
            "ok": len(duplicate_dedup) == 0,
            "detail": f"{len(duplicate_dedup)} 组重复去重键",
        },
        "invalid_status": {
            "ok": len(invalid_status) == 0,
            "detail": f"{len(invalid_status)} 条非法状态",
        },
        "low_score_pending": {
            "ok": not low_score_pending or int(low_score_pending["cnt"]) == 0,
            "detail": f"{int(low_score_pending['cnt']) if low_score_pending else 0} 条低分岗位仍在待投递",
        },
        "missing_url_pending": {
            "ok": not missing_url_pending or int(missing_url_pending["cnt"]) == 0,
            "detail": f"{int(missing_url_pending['cnt']) if missing_url_pending else 0} 条待投递缺少链接",
        },
        "stale_pending": {
            "ok": not stale_pending or int(stale_pending["cnt"]) == 0,
            "detail": f"{int(stale_pending['cnt']) if stale_pending else 0} 条待投递超过 7 天未处理",
        },
    }
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks, "profile": profile}
