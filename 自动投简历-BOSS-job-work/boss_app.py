#!/usr/bin/env python3
"""
BOSS直聘自动化控制台 —— FastAPI 后端
提供 REST API + WebSocket + 后台监控循环。
用法: python boss_app.py --port 8000
"""

import argparse
import asyncio
import contextlib
import json
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List
from urllib.parse import urljoin
from queue import Queue

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from boss_automation import BossAutomation
from boss_state import (
    add_application,
    get_application,
    get_application_by_url,
    get_application_by_dedup_key,
    update_application_from_job,
    update_application_intelligence,
    list_applications,
    clear_pending_applications,
    update_application_status,
    get_today_application_count,
    get_or_create_conversation,
    get_conversation,
    list_active_conversations,
    add_message,
    get_messages,
    replace_conversation_messages,
    update_conversation_last_message,
    update_conversation_status,
    set_auto_reply,
    get_setting,
    set_setting,
    get_all_settings,
    get_daily_stats,
    get_wechat_exchanges,
    get_today_pending_count,
    count_hours_replied_in_range,
    count_interest_level,
    add_to_shortlist,
    remove_from_shortlist,
    list_shortlists,
    is_in_shortlist,
)
from boss_replier import generate_greeting
from boss_job_intelligence import (
    enrich_job,
    load_candidate_profile,
    run_data_doctor,
)

# ── FastAPI 应用 ──
app = FastAPI(title="BOSS直聘自动化控制台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ── 全局状态 ──
automation: Optional[BossAutomation] = None
monitor_task: Optional[asyncio.Task] = None
ws_clients: List[WebSocket] = []
monitor_paused: bool = False
browser_sync_lock: Optional[asyncio.Lock] = None
browser_lifecycle_lock: Optional[asyncio.Lock] = None


@app.on_event("startup")
async def on_startup():
    global automation, monitor_task, browser_sync_lock, browser_lifecycle_lock
    browser_sync_lock = asyncio.Lock()
    browser_lifecycle_lock = asyncio.Lock()
    # 清理旧垃圾会话 + 合并同名重复会话
    try:
        from boss_state import get_db

        db = get_db()
        junk_names = [
            "HR",
            "你好",
            "消息",
            "未知HR",
            "AI简历",
            "简历更新",
            "附件简历制作",
            "附件上传",
        ]
        for name in junk_names:
            db.execute("DELETE FROM conversations WHERE hr_name = ?", (name,))
        db.execute("DELETE FROM conversations WHERE hr_name IS NULL OR length(hr_name) < 2")
        # 合并同名重复：保留最早的，把重复的改成 closed
        db.execute("""
            UPDATE conversations SET status = 'closed'
            WHERE id NOT IN (
                SELECT MIN(id) FROM conversations WHERE status != 'closed' GROUP BY hr_name
            ) AND status != 'closed'
        """)
        db.commit()
    except Exception:
        pass
    if automation is not None and automation.page is not None:
        monitor_task = asyncio.create_task(chat_monitor_loop())


class _PlaywrightWorker:
    """把所有 Playwright Sync API 调用固定到一个独立线程。"""

    def __init__(self):
        self._queue = Queue()
        self._thread = threading.Thread(target=self._loop, name="pw-worker", daemon=True)
        self._thread.start()

    def _loop(self):
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass

        while True:
            item = self._queue.get()
            if item is None:
                break

            fn, args, loop, future = item
            if future.cancelled():
                continue

            try:
                try:
                    asyncio.set_event_loop(None)
                except Exception:
                    pass
                result = fn(*args)
            except Exception as exc:
                if not future.done():
                    loop.call_soon_threadsafe(future.set_exception, exc)
            else:
                if not future.done():
                    loop.call_soon_threadsafe(future.set_result, result)

    async def run(self, fn, *args):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._queue.put((fn, args, loop, future))
        return await future


_playwright_worker = _PlaywrightWorker()


async def _run_pw(fn, *args):
    return await _playwright_worker.run(fn, *args)


def _browser_lock() -> asyncio.Lock:
    global browser_sync_lock
    if browser_sync_lock is None:
        browser_sync_lock = asyncio.Lock()
    return browser_sync_lock


def _browser_lifecycle_lock() -> asyncio.Lock:
    global browser_lifecycle_lock
    if browser_lifecycle_lock is None:
        browser_lifecycle_lock = asyncio.Lock()
    return browser_lifecycle_lock


async def _cancel_monitor_task():
    global monitor_task
    task = monitor_task
    monitor_task = None
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _close_automation_instance(instance: Optional[BossAutomation], save_state: bool = True):
    if not instance:
        return
    if save_state:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_run_pw(instance._save_state), timeout=8)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(_run_pw(instance.close), timeout=15)
    _kill_browser_profile_processes()
    _cleanup_browser_profile_locks()


def _browser_busy_message(action: str = "操作") -> str:
    return f"浏览器正在执行其他任务，已跳过本次{action}，请稍后再试"


def _cleanup_browser_profile_locks():
    try:
        from boss_firefox import PROFILE_DIR

        for name in ("parent.lock", ".parentlock", ".startup-incomplete", "lock"):
            path = PROFILE_DIR / name
            if path.exists():
                with contextlib.suppress(Exception):
                    path.unlink()
    except Exception:
        pass


def _kill_browser_profile_processes():
    if sys.platform != "win32":
        return
    try:
        from boss_firefox import PROFILE_DIR

        profile = str(PROFILE_DIR)
        ps = f"""
$profile = '{profile}'
Get-CimInstance Win32_Process | Where-Object {{
    $_.Name -eq 'firefox.exe' -and $_.CommandLine -like "*$profile*"
}} | ForEach-Object {{
    try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }} catch {{}}
}}
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        pass


# BOSS直聘城市代码（按省份分组）
CITY_MAP = {
    # 山东省
    "济南": "101120100",
    "青岛": "101120200",
    "淄博": "101120300",
    "德州": "101120400",
    "烟台": "101120500",
    "潍坊": "101120600",
    "济宁": "101120700",
    "泰安": "101120800",
    "临沂": "101120900",
    "菏泽": "101121000",
    "滨州": "101121100",
    "东营": "101121200",
    "威海": "101121300",
    "枣庄": "101121400",
    "日照": "101121500",
    "聊城": "101121700",
    # 一线城市
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    # 新一线城市
    "成都": "101270100",
    "杭州": "101210100",
    "武汉": "101200100",
    "南京": "101190100",
    "重庆": "101040100",
    "西安": "101110100",
    "长沙": "101250100",
    "天津": "101030100",
    "苏州": "101190400",
    "郑州": "101180100",
    "东莞": "101281600",
    "沈阳": "101070100",
    "宁波": "101210400",
    "昆明": "101290100",
    # 其他省会城市
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "南昌": "101240100",
    "贵阳": "101260100",
    "南宁": "101300100",
    "太原": "101100100",
    "石家庄": "101090100",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "兰州": "101160100",
    "乌鲁木齐": "101130100",
    "呼和浩特": "101080100",
    "拉萨": "101140100",
    "西宁": "101150100",
    "银川": "101170100",
    "海口": "101310100",
    "三亚": "101310200",
    # 特殊选项
    "全国": "100010000",
}

CITY_DISTRICTS = {
    "北京": ("朝阳", "海淀", "丰台", "昌平", "大兴", "通州", "西城", "东城", "石景山", "顺义", "房山", "怀柔", "密云", "延庆", "门头沟", "平谷"),
    "上海": ("浦东", "杨浦", "闵行", "徐汇", "长宁", "静安", "黄浦", "普陀", "虹口", "宝山", "嘉定", "松江", "青浦", "奉贤", "金山", "崇明"),
    "广州": ("天河", "越秀", "海珠", "白云", "黄埔", "番禺", "花都", "南沙", "增城", "从化", "荔湾"),
    "深圳": ("南山", "福田", "宝安", "龙岗", "龙华", "罗湖", "盐田", "光明", "坪山", "大鹏"),
    "杭州": ("西湖", "滨江", "余杭", "萧山", "拱墅", "上城", "临平", "钱塘", "富阳", "临安"),
    "武汉": ("江夏", "洪山", "武昌", "汉阳", "江汉", "硚口", "青山", "东西湖", "蔡甸", "黄陂", "新洲"),
    "成都": ("武侯", "锦江", "青羊", "金牛", "成华", "高新", "天府新区", "双流", "龙泉驿", "郫都", "温江"),
    "南京": ("玄武", "秦淮", "建邺", "鼓楼", "浦口", "栖霞", "雨花台", "江宁", "六合", "溧水", "高淳"),
    "苏州": ("姑苏", "吴中", "相城", "虎丘", "吴江", "工业园", "昆山", "常熟", "张家港", "太仓"),
    "合肥": ("蜀山", "包河", "庐阳", "瑶海", "高新", "经开", "肥西", "肥东", "长丰"),
    "西安": ("雁塔", "碑林", "莲湖", "未央", "新城", "灞桥", "长安", "高新", "经开"),
    "天津": ("和平", "河西", "南开", "河东", "河北", "红桥", "滨海", "西青", "津南", "北辰", "东丽"),
    "重庆": ("渝北", "渝中", "江北", "南岸", "九龙坡", "沙坪坝", "巴南", "北碚", "大渡口"),
    "长沙": ("岳麓", "芙蓉", "天心", "开福", "雨花", "望城", "长沙县"),
    "郑州": ("金水", "中原", "二七", "管城", "惠济", "郑东", "高新", "经开"),
    "东莞": ("南城", "东城", "莞城", "万江", "松山湖", "虎门", "长安", "厚街", "塘厦"),
    "宁波": ("鄞州", "海曙", "江北", "镇海", "北仑", "奉化", "余姚", "慈溪"),
    "厦门": ("思明", "湖里", "集美", "海沧", "同安", "翔安"),
    "福州": ("鼓楼", "台江", "仓山", "晋安", "马尾", "长乐", "闽侯"),
}


def _job_matches_city(job_city: str, selected_city: Optional[str]) -> bool:
    city = (selected_city or "").strip()
    text = (job_city or "").strip()
    if not city or city == "全国" or not text:
        return True
    if city in text:
        return True
    selected_districts = CITY_DISTRICTS.get(city, ())
    if any(d in text for d in selected_districts):
        return True

    for other_city, districts in CITY_DISTRICTS.items():
        if other_city == city:
            continue
        if other_city in text or any(d in text for d in districts):
            return False

    other_city_hit = next((c for c in CITY_MAP.keys() if c not in (city, "全国") and c in text), "")
    if other_city_hit:
        return False
    return False


def _normalize_job_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return urljoin("https://www.zhipin.com", url)


def _split_filter_terms(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip().lower() for t in re.split(r"[,，、\n\r]+", str(raw)) if t.strip()]


def _split_search_keywords(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in re.split(r"[,，、\n\r]+", str(raw)) if t.strip()]


def _job_search_text(job: dict) -> str:
    return " ".join(
        str(job.get(k, "") or "")
        for k in ("title", "job_title", "company", "description", "hr_name", "hr_title", "city")
    ).lower()


def _job_title_search_text(job: dict) -> str:
    return " ".join(str(job.get(k, "") or "") for k in ("title", "job_title")).lower()


def _job_matches_required_keywords(job: dict, keywords: Optional[list[str]]) -> bool:
    terms = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not terms:
        return True
    haystack = _job_title_search_text(job)
    return any(term in haystack for term in terms)


def _daily_apply_limit() -> int:
    try:
        return max(1, int(get_setting("daily_apply_limit", "15") or "15"))
    except (TypeError, ValueError):
        return 15


def _salary_bounds_k(text: str) -> Optional[tuple[float, float]]:
    """Parse common BOSS salary strings into K/month lower and upper bounds."""
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


def _filter_jobs(
    jobs: list[dict],
    salary_min: Optional[float] = None,
    exclude_keywords: Optional[str] = None,
    city: Optional[str] = None,
    required_keywords: Optional[list[str]] = None,
) -> tuple[list[dict], dict]:
    terms = _split_filter_terms(exclude_keywords)
    selected_city = (city or "").strip()
    stats = {"salary_min": 0, "keywords": 0, "city": 0, "search_keywords": 0}
    filtered = []
    for job in jobs:
        if selected_city and selected_city != "全国":
            job_city = str(job.get("city", "") or "").strip()
            if not _job_matches_city(job_city, selected_city):
                stats["city"] += 1
                continue

        if not _job_matches_required_keywords(job, required_keywords):
            stats["search_keywords"] += 1
            continue

        if salary_min is not None:
            bounds = _salary_bounds_k(job.get("salary", ""))
            if not bounds or bounds[0] < salary_min:
                stats["salary_min"] += 1
                continue

        if terms:
            haystack = _job_search_text(job)
            if any(term in haystack for term in terms):
                stats["keywords"] += 1
                continue

        filtered.append(job)
    return filtered, stats


def _score_jobs(
    jobs: list[dict],
    city: Optional[str] = None,
    salary_min: Optional[float] = None,
    exclude_keywords: Optional[str] = None,
    required_keywords: Optional[list[str]] = None,
) -> tuple[list[dict], list[str]]:
    profile, issues = load_candidate_profile(get_all_settings())
    scored = [
        enrich_job(
            job,
            profile,
            selected_city=city or "",
            salary_min=salary_min,
            exclude_keywords=exclude_keywords or "",
            required_keywords=required_keywords or [],
        )
        for job in jobs
    ]
    return scored, issues


def _upsert_scored_job(job: dict) -> tuple[Optional[int], dict]:
    """按 URL 或去重键保存岗位，返回应用 id 与完整记录。"""
    url = _normalize_job_url(job.get("url") or job.get("job_url") or "")
    job["url"] = url
    existing = get_application_by_url(url) if url else None
    if not existing:
        existing = get_application_by_dedup_key(job.get("dedup_key", ""))
    if existing:
        update_application_from_job(existing["id"], job)
        updated = update_application_intelligence(existing["id"], job) or existing
        return updated.get("id"), updated
    aid = add_application(job)
    if aid:
        return aid, get_application(aid) or {}
    return None, {}


def _application_matches_filters(
    app: Optional[dict],
    salary_min: Optional[float] = None,
    exclude_keywords: Optional[str] = None,
    city: Optional[str] = None,
) -> tuple[bool, str]:
    if not app:
        return True, ""
    filtered, stats = _filter_jobs([app], salary_min, exclude_keywords, city)
    if stats.get("city"):
        return False, "岗位城市不符合当前筛选，已跳过"
    if stats.get("keywords"):
        return False, "岗位命中屏蔽关键词，已跳过"
    if stats.get("salary_min"):
        return False, "岗位薪资低于最低薪资筛选，已跳过"
    if not filtered:
        return False, "岗位不符合当前筛选条件，已跳过"
    try:
        min_fit_score = int(float(get_setting("min_fit_score", "60") or "60"))
    except (TypeError, ValueError):
        min_fit_score = 60
    fit_score = int(app.get("fit_score") or 0)
    if app.get("fit_recommendation") == "skip" or (fit_score and fit_score < min_fit_score):
        reason = app.get("fit_reason") or f"岗位评分低于 {min_fit_score}"
        return False, f"岗位匹配度不足({fit_score}分)，{reason}"
    return True, ""


def _search_job_payload(job: dict, application: Optional[dict] = None) -> dict:
    """统一搜索结果和数据库记录的字段名，方便前端直接渲染。"""
    application = application or {}
    return {
        "id": application.get("id"),
        "job_title": application.get("job_title") or job.get("title", ""),
        "company": application.get("company") or job.get("company", ""),
        "salary": application.get("salary") or job.get("salary", ""),
        "job_url": application.get("job_url") or _normalize_job_url(job.get("url", "")),
        "city": application.get("city") or job.get("city", ""),
        "experience": application.get("experience") or job.get("experience", ""),
        "education": application.get("education") or job.get("education", ""),
        "hr_name": application.get("hr_name") or job.get("hr_name", ""),
        "hr_title": application.get("hr_title") or job.get("hr_title", ""),
        "description": application.get("description") or job.get("description", ""),
        "status": application.get("status") or ("pending" if job.get("url") else "missing_url"),
        "dedup_key": application.get("dedup_key") or job.get("dedup_key", ""),
        "fit_score": application.get("fit_score") if application.get("fit_score") is not None else job.get("fit_score", 0),
        "fit_level": application.get("fit_level") or job.get("fit_level", ""),
        "fit_recommendation": application.get("fit_recommendation") or job.get("fit_recommendation", ""),
        "fit_reason": application.get("fit_reason") or job.get("fit_reason", ""),
        "fit_detail": application.get("fit_detail") or job.get("fit_detail", ""),
    }


def _clean_messages_for_web(messages: List[dict]) -> List[dict]:
    """清理 BOSS DOM 里混入的已读/送达状态，保持 Web 端只展示聊天正文。"""
    cleaned = []
    status_words = ("已读", "未读", "送达", "发送失败", "已发送")
    for msg in messages:
        item = dict(msg)
        content = (item.get("content") or "").strip()
        for word in status_words:
            if content.startswith(word):
                content = content[len(word) :].strip()
            if content.endswith(word):
                content = content[: -len(word)].strip()
        item["content"] = content
        if content:
            cleaned.append(item)
    return cleaned


# ══════════════════════════════════════
#  Pydantic Models
# ══════════════════════════════════════


class SearchRequest(BaseModel):
    keyword: str = "AI Agent"
    city: str = ""
    welfare: Optional[str] = None
    salary_min: Optional[float] = None
    exclude_keywords: Optional[str] = None
    limit: int = 600


class ApplyRequest(BaseModel):
    job_url: str
    greeting: Optional[str] = None
    salary_min: Optional[float] = None
    exclude_keywords: Optional[str] = None
    city: Optional[str] = None


class ApplyBatchRequest(BaseModel):
    job_urls: List[str]
    greeting: Optional[str] = None
    salary_min: Optional[float] = None
    exclude_keywords: Optional[str] = None
    city: Optional[str] = None


class ScanAndApplyRequest(BaseModel):
    greeting: Optional[str] = None


class AnalyzeRequest(BaseModel):
    job_url: str
    job_title: Optional[str] = ""
    company: Optional[str] = ""
    description: Optional[str] = ""


class SendMessageRequest(BaseModel):
    content: str


class AutoReplyToggleRequest(BaseModel):
    enabled: bool


class SyncConversationsRequest(BaseModel):
    limit: int = 50


class SettingsUpdate(BaseModel):
    greeting_template: Optional[str] = None
    greeting_enabled: Optional[str] = None
    ai_reply_style: Optional[str] = None
    ai_reply_decision_enabled: Optional[str] = None
    user_reply_style_profile: Optional[str] = None
    daily_apply_limit: Optional[str] = None
    auto_reply_enabled: Optional[str] = None
    min_reply_delay_sec: Optional[str] = None
    max_reply_delay_sec: Optional[str] = None
    batch_delay_min_sec: Optional[str] = None
    batch_delay_max_sec: Optional[str] = None
    resume_summary: Optional[str] = None
    wechat_id: Optional[str] = None
    search_keywords: Optional[str] = None  # 逗号分隔的搜索关键词
    default_city: Optional[str] = None  # 默认搜索城市
    min_fit_score: Optional[str] = None  # 岗位评分投递阈值
    candidate_profile_json: Optional[str] = None  # 求职偏好 Profile JSON
    selector_overrides: Optional[str] = None  # JSON 格式的选择器覆盖
    ai_api_key: Optional[str] = None  # AI API Key
    ai_base_url: Optional[str] = None  # AI Base URL
    ai_model: Optional[str] = None  # AI 模型名称


# ══════════════════════════════════════
#  WebSocket 广播
# ══════════════════════════════════════


async def broadcast_ws(message: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


# ══════════════════════════════════════
#  页面
# ══════════════════════════════════════


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = static_dir / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>BOSS直聘自动化控制台</h1><p>dashboard.html 未找到</p>")


# ══════════════════════════════════════
#  系统状态
# ══════════════════════════════════════


@app.get("/api/status")
def get_status():
    browser_ok = automation is not None and automation.page is not None
    return {
        "browser_running": browser_ok,
        "auto_reply_enabled": get_setting("auto_reply_enabled", "false") == "true",
        "monitor_running": monitor_task is not None and not monitor_task.done(),
        "monitor_paused": monitor_paused,
        "today_applications": get_today_application_count(),
        "daily_apply_limit": _daily_apply_limit(),
        "active_conversations": len(list_active_conversations()),
        "daily_stats": get_daily_stats(),
    }


@app.get("/api/stats")
def get_stats():
    """投递转化漏斗统计。"""
    today = get_daily_stats()
    return {
        "today_applications": get_today_application_count(),
        "daily_apply_limit": _daily_apply_limit(),
        "pending": get_today_pending_count(),
        "replied": count_hours_replied_in_range(24),
        "interview": count_interest_level("high"),
        "active_conversations": len(list_active_conversations()),
        "daily_stats": today,
    }


@app.get("/api/doctor")
def doctor():
    """诊断环境：Python版本、浏览器状态、登录态、AI配置等。"""
    import os
    import sys as _sys
    from boss_state import get_db

    try:
        _sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import _load_ai_config

        cfg = _load_ai_config()
        ai_key_ok = bool(cfg.get("api_key") and len(cfg["api_key"]) > 10)
    except Exception:
        ai_key_ok = False

    browser_ok = automation is not None and automation.page is not None
    checks = {
        "python": {"ok": True, "detail": _sys.version.split()[0]},
        "browser": {"ok": browser_ok, "detail": "运行中" if browser_ok else "未启动"},
        "boss_login": {"ok": browser_ok, "detail": "已登录" if browser_ok else "未登录"},
        "ai_key": {"ok": ai_key_ok, "detail": "已配置" if ai_key_ok else "未配置"},
        "today_applications": get_today_application_count(),
        "pending_jobs": get_today_pending_count(),
    }
    data_doctor = run_data_doctor(get_db, get_all_settings())
    checks.update({f"data_{k}": v for k, v in data_doctor["checks"].items()})
    all_ok = all(v.get("ok", True) for v in checks.values() if isinstance(v, dict))
    return {"ok": all_ok, "checks": checks, "job_profile": data_doctor.get("profile", {})}


@app.post("/api/system/start")
async def start_automation():
    global automation, monitor_task, monitor_paused
    async with _browser_lifecycle_lock():
        if automation is not None and automation.page is not None:
            return {"status": "already_started"}

        await _cancel_monitor_task()

        def _do_start():
            a = BossAutomation(headless=False)
            a.start()
            return a

        try:
            automation = await _run_pw(_do_start)
        except Exception as e:
            automation = None
            return {"status": "error", "message": f"浏览器启动失败: {e}"}

        if automation is None or automation.page is None:
            automation = None
            return {"status": "error", "message": "浏览器启动后页面为空，请重试"}

        monitor_paused = False
        if monitor_task is None or monitor_task.done():
            monitor_task = asyncio.create_task(chat_monitor_loop())
    await broadcast_ws({"type": "system", "event": "started"})
    return {"status": "started"}


@app.post("/api/system/stop")
async def stop_automation():
    global automation, monitor_paused
    async with _browser_lifecycle_lock():
        await _cancel_monitor_task()
        old_automation = automation
        automation = None
        monitor_paused = False
        await _close_automation_instance(old_automation, save_state=True)
    await broadcast_ws({"type": "system", "event": "stopped"})
    return {"status": "stopped"}


@app.post("/api/system/relogin")
async def relogin():
    """重新登录 BOSS直聘。会打开浏览器让用户扫码。"""
    global automation, monitor_task, monitor_paused
    async with _browser_lifecycle_lock():
        await _cancel_monitor_task()
        await _close_automation_instance(automation, save_state=False)
        automation = None

        def _do_relogin():
            a = BossAutomation(headless=False)
            a.start()
            a.login()
            return a

        try:
            automation = await _run_pw(_do_relogin)
        except Exception as e:
            automation = None
            return {"status": "error", "message": f"登录失败: {e}"}

        if automation is None or automation.page is None:
            automation = None
            return {"status": "error", "message": "登录后页面异常，请重试"}

        monitor_paused = False
        if monitor_task is None or monitor_task.done():
            monitor_task = asyncio.create_task(chat_monitor_loop())
    await broadcast_ws({"type": "system", "event": "relogin_ok"})
    return {"status": "ok", "message": "扫码登录成功"}


@app.post("/api/system/heartbeat")
async def manual_heartbeat():
    """手动心跳保活。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    lock = _browser_lock()
    if lock.locked():
        raise HTTPException(status_code=409, detail=_browser_busy_message("登录态检测"))
    async with lock:
        alive = await _run_pw(automation.heartbeat)
    if not alive:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return {"status": "ok", "alive": True}


@app.post("/api/monitor/pause")
async def pause_monitor():
    global monitor_paused
    monitor_paused = True
    await broadcast_ws({"type": "monitor_paused"})
    return {"status": "paused"}


@app.post("/api/monitor/resume")
async def resume_monitor():
    global monitor_paused, monitor_task
    monitor_paused = False
    if automation is not None and automation.page is not None and (monitor_task is None or monitor_task.done()):
        monitor_task = asyncio.create_task(chat_monitor_loop())
    await broadcast_ws({"type": "monitor_resumed"})
    return {"status": "resumed", "monitor_running": monitor_task is not None and not monitor_task.done()}


@app.post("/api/monitor/start")
async def start_monitor():
    global monitor_paused, monitor_task
    if automation is None or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    monitor_paused = False
    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(chat_monitor_loop())
    await broadcast_ws({"type": "monitor_resumed"})
    return {"status": "started", "monitor_running": True}


@app.post("/api/system/navigate-chat")
async def navigate_to_chat_page():
    """在浏览器中打开 BOSS 直聘聊天页。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    lock = _browser_lock()
    if lock.locked():
        return {"status": "busy", "message": _browser_busy_message("打开聊天页")}
    async with lock:
        success = await _run_pw(automation.navigate_to_chat)
    return {
        "status": "ok" if success else "error",
        "message": "已跳转到聊天页" if success else "跳转失败，请检查登录状态",
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "browser": automation is not None}


# ══════════════════════════════════════
#  调试 / 页面分析（BOSS改版时诊断选择器）
# ══════════════════════════════════════


class SelectorTest(BaseModel):
    selector: str


@app.post("/api/debug/selector-test")
async def test_selector(req: SelectorTest):
    """测试任意 CSS 选择器，返回匹配元素数和文本。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    result = await _run_pw(
        lambda: automation.page.evaluate(
            """(sel) => {
            try {
                const els = document.querySelectorAll(sel);
                const items = [];
                for (let i = 0; i < Math.min(els.length, 10); i++) {
                    items.push((els[i].innerText || '').trim().substring(0, 200));
                }
                return {count: els.length, samples: items};
            } catch(e) {
                return {error: e.message};
            }
        }""",
            req.selector,
        )
    )
    return result


@app.get("/api/debug/page-stats")
async def page_stats():
    """返回当前页面 DOM 统计，帮助诊断选择器失效。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    result = await _run_pw(
        lambda: automation.page.evaluate("""() => {
        const stats = {};
        stats.url = window.location.href;
        stats.title = document.title;
        stats.bodyLength = (document.body?.innerText || '').length;
        // 关键元素计数
        stats.liCount = document.querySelectorAll('li').length;
        stats.inputCount = document.querySelectorAll('input, textarea, [contenteditable]').length;
        stats.buttonCount = document.querySelectorAll('button').length;
        stats.messageItems = document.querySelectorAll('li.message-item, [class*="message-item"]').length;
        stats.listItems = document.querySelectorAll('li[role="listitem"]').length;
        stats.chatInput = document.querySelector('#chat-input') ? 1 : 0;
        stats.sendButton = document.querySelector('button[type="send"]') ? 1 : 0;
        // body 前 500 字符
        stats.bodyPreview = (document.body?.innerText || '').substring(0, 500);
        return stats;
    }""")
    )
    return result


@app.get("/api/debug/selectors-status")
async def selectors_status():
    """检查所有关键选择器的有效性。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    from boss_automation import SELECTORS

    result = await _run_pw(
        lambda: automation.page.evaluate(
            """(groups) => {
            const res = {};
            for (const [key, sels] of Object.entries(groups)) {
                for (const sel of sels) {
                    try {
                        const count = document.querySelectorAll(sel).length;
                        if (count > 0) {
                            res[key] = {selector: sel, count: count, ok: true};
                            break;
                        }
                    } catch(e) {}
                }
                if (!res[key]) res[key] = {selector: sels[sels.length-1], count: 0, ok: false};
            }
            return res;
        }""",
            SELECTORS,
        )
    )
    return result


# ══════════════════════════════════════
#  岗位搜索 & 管理
# ══════════════════════════════════════


@app.get("/api/jobs")
def list_jobs(
    status: Optional[str] = None,
    limit: int = 100,
    keyword: Optional[str] = None,
    city: Optional[str] = None,
    salary_min: Optional[float] = None,
    exclude_keywords: Optional[str] = None,
):
    required_keywords = _split_search_keywords(keyword)
    has_filters = (
        salary_min is not None
        or bool(exclude_keywords)
        or bool(city and city != "全国")
        or bool(required_keywords)
    )
    fetch_limit = max(limit * 5, 2000) if has_filters else limit
    jobs = list_applications(status, fetch_limit)
    raw_total = len(jobs)
    filtered_out = {"salary_min": 0, "keywords": 0, "city": 0, "search_keywords": 0}
    if has_filters:
        jobs, filtered_out = _filter_jobs(jobs, salary_min, exclude_keywords, city, required_keywords)
        jobs = jobs[:limit]
    return {"jobs": jobs, "total": len(jobs), "raw_total": raw_total, "filtered_out": filtered_out}


@app.post("/api/jobs/search")
async def search_jobs(req: SearchRequest):
    global monitor_paused
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动，请先到设置Tab点击「启动浏览器」")
    lock = _browser_lock()
    was_paused = monitor_paused
    monitor_paused = True
    try:
        city_code = CITY_MAP.get(req.city or get_setting("default_city", "全国"), "100010000")
        keywords = _split_search_keywords(req.keyword) or [req.keyword.strip() or "AI Agent"]
        try:
            await asyncio.wait_for(lock.acquire(), timeout=60)
        except asyncio.TimeoutError:
            return {
                "success": False,
                "detail": "浏览器正在处理聊天监控，已临时暂停监控但仍未空闲，请稍后再搜",
                "jobs_found": 0,
                "saved": 0,
                "jobs": [],
            }
        try:
            jobs = []
            seen_urls = set()
            per_keyword_limit = max(50, req.limit)
            for keyword in keywords:
                keyword_jobs = await _run_pw(automation.search, keyword, city_code, per_keyword_limit)
                for job in keyword_jobs:
                    url = _normalize_job_url(job.get("url", ""))
                    key = url or f"{job.get('title','')}|{job.get('company','')}|{job.get('city','')}"
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    job["url"] = url
                    job["search_keyword"] = keyword
                    jobs.append(job)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"搜索失败: {e}")
        finally:
            lock.release()

        # 福利筛选
        if req.welfare:
            welfare_kw = [w.strip() for w in req.welfare.split(",") if w.strip()]
            jobs = automation._filter_by_welfare(jobs, welfare_kw)

        raw_found = len(jobs)
        filtered_out = {"salary_min": 0, "keywords": 0, "city": 0, "search_keywords": 0, "low_fit": 0}
        if req.salary_min is not None or req.exclude_keywords or req.city or keywords:
            jobs, filtered_out = _filter_jobs(jobs, req.salary_min, req.exclude_keywords, req.city, keywords)
            filtered_out["low_fit"] = 0

        jobs, profile_issues = _score_jobs(jobs, req.city, req.salary_min, req.exclude_keywords, keywords)
        filtered_out["low_fit"] = sum(1 for j in jobs if j.get("fit_recommendation") == "skip")

        saved_ids = []
        result_jobs = []
        for j in jobs:
            aid, saved = _upsert_scored_job(j)
            if aid:
                saved_ids.append(aid)
                result_jobs.append(_search_job_payload(j, saved))
            else:
                result_jobs.append(_search_job_payload(j))

        await broadcast_ws(
            {
                "type": "search_complete",
                "keyword": req.keyword,
                "city": req.city,
                "found": len(jobs),
            }
        )
        return {
            "jobs_found": len(jobs),
            "raw_found": raw_found,
            "filtered_out": filtered_out,
            "profile_issues": profile_issues,
            "saved": len(saved_ids),
            "jobs": result_jobs,
        }
    finally:
        monitor_paused = was_paused


@app.delete("/api/jobs/pending")
async def clear_pending_jobs():
    deleted = clear_pending_applications()
    await broadcast_ws({"type": "pending_cleared", "deleted": deleted})
    return {"status": "ok", "deleted": deleted}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = get_application(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")
    return {"job": job}


@app.post("/api/jobs/{job_id}/skip")
async def skip_job(job_id: int):
    update_application_status(job_id, "skipped")
    await broadcast_ws({"type": "job_updated", "job_id": job_id, "status": "skipped"})
    return {"status": "ok"}


# ══════════════════════════════════════
#  投递
# ══════════════════════════════════════


@app.post("/api/jobs/apply")
async def apply_to_job(req: ApplyRequest):
    global monitor_paused
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    lock = _browser_lock()

    daily_limit = _daily_apply_limit()
    if get_today_application_count() >= daily_limit:
        raise HTTPException(status_code=429, detail="已达到今日投递上限")

    job = get_application_by_url(req.job_url)
    matches, skip_reason = _application_matches_filters(job, req.salary_min, req.exclude_keywords, req.city)
    if not matches:
        return {"success": False, "message": skip_reason, "filtered": True}

    greeting = req.greeting
    if not greeting:
        title = job["job_title"] if job else "相关岗位"
        company = job["company"] if job else "贵公司"
        style = get_setting("ai_reply_style", "professional")
        greeting = generate_greeting(title, company, style=style)

    was_paused = monitor_paused
    monitor_paused = True
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=180)
        except asyncio.TimeoutError:
            return {
                "success": False,
                "message": "浏览器正在处理聊天监控，已临时暂停监控但仍未空闲，请稍后再投递",
            }

        try:
            result = await _run_pw(automation.apply_to_job, req.job_url, greeting)
        finally:
            lock.release()
    finally:
        monitor_paused = was_paused

    if result.get("success"):
        await broadcast_ws(
            {
                "type": "apply_complete",
                "job_url": req.job_url,
                "job_id": result.get("application_id"),
            }
        )
    return result


@app.post("/api/jobs/apply-batch")
async def apply_batch(req: ApplyBatchRequest):
    global monitor_paused
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    lock = _browser_lock()

    daily_limit = _daily_apply_limit()
    remaining = daily_limit - get_today_application_count()
    urls = []
    skipped = []
    for url in req.job_urls:
        matches, skip_reason = _application_matches_filters(
            get_application_by_url(url), req.salary_min, req.exclude_keywords, req.city
        )
        if matches:
            urls.append(url)
        else:
            skipped.append({"success": False, "message": skip_reason, "job_url": url, "filtered": True})
    if remaining <= 0:
        return {
            "results": skipped
            + [
                {"success": False, "message": "已达到今日投递上限", "job_url": url}
                for url in urls
            ]
        }
    urls = urls[:remaining]
    if not urls:
        return {"results": skipped}

    was_paused = monitor_paused
    monitor_paused = True
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=180)
        except asyncio.TimeoutError:
            return {
                "results": skipped
                + [
                    {
                        "success": False,
                        "message": "浏览器正在处理聊天监控，已临时暂停监控但仍未空闲，请稍后再批量投递",
                        "job_url": url,
                    }
                    for url in urls
                ]
            }

        try:
            results = await _run_pw(automation.apply_batch, urls, req.greeting)
        finally:
            lock.release()
    finally:
        monitor_paused = was_paused

    results = skipped + results
    await broadcast_ws(
        {
            "type": "batch_complete",
            "total": len(results),
            "success": sum(1 for r in results if r.get("success")),
        }
    )
    return {"results": results}


@app.post("/api/jobs/scan")
async def scan_current_page():
    """扫描当前BOSS搜索结果页面，提取所有可见岗位，保存到数据库并返回。"""
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动，请先到设置Tab点击「启动浏览器」")
    lock = _browser_lock()
    if lock.locked():
        return {"success": False, "detail": _browser_busy_message("岗位扫描"), "jobs_found": 0, "saved": 0, "jobs": []}

    try:
        async with lock:
            jobs = await _run_pw(automation.scan_current_page)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描失败: {e}")

    saved_ids = []
    result_jobs = []
    jobs, profile_issues = _score_jobs(jobs)
    for j in jobs:
        aid, saved = _upsert_scored_job(j)
        if aid:
            saved_ids.append(aid)
            result_jobs.append(_search_job_payload(j, saved))
        else:
            result_jobs.append(_search_job_payload(j))

    await broadcast_ws(
        {
            "type": "scan_complete",
            "found": len(jobs),
            "saved": len(saved_ids),
        }
    )
    return {"jobs_found": len(jobs), "saved": len(saved_ids), "profile_issues": profile_issues, "jobs": result_jobs}


@app.post("/api/jobs/scan-and-apply")
async def scan_and_apply(req: ScanAndApplyRequest = ScanAndApplyRequest()):
    """扫描当前页面全部岗位 → 一键批量投递。"""
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    lock = _browser_lock()
    if lock.locked():
        return {"success": False, "message": _browser_busy_message("扫描投递"), "scanned": 0, "applied": 0}

    daily_limit = _daily_apply_limit()
    if get_today_application_count() >= daily_limit:
        raise HTTPException(status_code=429, detail="已达到今日投递上限")

    async with lock:
        jobs = await _run_pw(automation.scan_current_page)
        scored_jobs, profile_issues = _score_jobs(jobs)
        saved_ids = []
        urls = []
        for job in scored_jobs:
            aid, saved = _upsert_scored_job(job)
            if aid:
                saved_ids.append(aid)
            if job.get("url") and job.get("fit_recommendation") != "skip":
                urls.append(_normalize_job_url(job.get("url", "")))
        remaining = max(0, daily_limit - get_today_application_count())
        urls = urls[:remaining]
        results = await _run_pw(automation.apply_batch, urls, req.greeting) if urls else []
        success_count = sum(1 for r in results if r.get("success"))
        result = {
            "success": success_count > 0,
            "message": f"扫描 {len(jobs)} 个岗位，保存 {len(saved_ids)} 个，评分跳过 {sum(1 for j in scored_jobs if j.get('fit_recommendation') == 'skip')} 个，投递 {success_count}/{len(urls)}",
            "scanned": len(jobs),
            "saved": len(saved_ids),
            "applied": success_count,
            "skipped_low_fit": sum(1 for j in scored_jobs if j.get("fit_recommendation") == "skip"),
            "profile_issues": profile_issues,
            "results": results,
        }
    await broadcast_ws(
        {
            "type": "scan_apply_complete",
            "scanned": result.get("scanned", 0),
            "applied": result.get("applied", 0),
        }
    )
    return result


@app.post("/api/jobs/analyze")
async def analyze_jd(req: AnalyzeRequest):
    """AI分析岗位JD，返回匹配度、关键技能、差距、建议。"""
    resume = get_setting("resume_summary", "")
    desc = req.description or ""
    title = req.job_title or ""
    company = req.company or ""

    if resume and len(resume.strip()) > 5:
        prompt = f"""你是求职辅导专家。分析以下岗位JD，对比求职者简历，输出JSON。

## 求职者简历
{resume}

## 岗位信息
- 公司: {company}
- 职位: {title}
- JD: {desc[:2000]}

## 输出格式（严格JSON）
{{
  "match_score": 85,
  "key_skills": ["Python", "LangChain", "RAG"],
  "gap": "缺少K8s部署经验",
  "advice": "建议强调Agent开发经验，问对方技术栈",
  "summary": "整体匹配度较高，注意补充部署相关经验"
}}"""
    else:
        prompt = f"""你是求职辅导专家。分析以下岗位JD，提取关键信息，输出JSON。

## 岗位信息
- 公司: {company}
- 职位: {title}
- JD: {desc[:2000]}

## 输出格式（严格JSON）
{{
  "match_score": 70,
  "key_skills": ["Python", "LangChain", "RAG"],
  "gap": "",
  "advice": "",
  "summary": "该岗位的核心要求是..."
}}

注意：match_score 基于 JD 难度和市场需求预估即可，不必对比简历。summary 用一两句总结这个岗位的核心要求。"""

    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import llm_chat_deepseek

        raw = llm_chat_deepseek(
            [{"role": "user", "content": prompt}],
            system_prompt="你是求职辅导专家，输出严格JSON。",
            temperature=0.3,
        )
        import json

        return json.loads(raw.strip().strip("`").strip("json").strip())
    except Exception as e:
        return {"error": f"AI分析失败: {e}", "match_score": 0, "summary": "请检查AI配置"}


@app.post("/api/jobs/rescore")
async def rescore_jobs():
    """按当前 profile 重新评分本地岗位池，并把低分待投递转为已跳过。"""
    jobs = list_applications(None, 5000)
    scored, profile_issues = _score_jobs(jobs)
    updated = 0
    skipped_low_fit = 0
    for job in scored:
        app_id = job.get("id")
        if not app_id:
            continue
        update_application_intelligence(int(app_id), job)
        updated += 1
        if job.get("fit_recommendation") == "skip":
            skipped_low_fit += 1
    await broadcast_ws({"type": "jobs_rescored", "updated": updated, "skipped_low_fit": skipped_low_fit})
    return {"status": "ok", "updated": updated, "skipped_low_fit": skipped_low_fit, "profile_issues": profile_issues}


# ══════════════════════════════════════
#  候选池
# ══════════════════════════════════════


@app.get("/api/shortlists")
def get_shortlists():
    return {"shortlists": list_shortlists()}


@app.post("/api/shortlists")
def add_shortlist(req: dict = {}):
    url = req.get("job_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="缺少 job_url")
    if is_in_shortlist(url):
        return {"status": "already_exists"}
    sid = add_to_shortlist(
        url,
        req.get("title", ""),
        req.get("company", ""),
        req.get("salary", ""),
        req.get("city", ""),
        req.get("note", ""),
    )
    if sid:
        return {"status": "ok", "id": sid}
    return {"status": "duplicate"}


@app.delete("/api/shortlists/{sid}")
def remove_shortlist(sid: int):
    remove_from_shortlist(sid)
    return {"status": "ok"}


# ══════════════════════════════════════
#  会话 & 聊天
# ══════════════════════════════════════


@app.get("/api/wechat-exchanges")
def list_wechat_exchanges():
    """返回所有已获取到 HR 微信号的会话。"""
    records = get_wechat_exchanges()
    return {"exchanges": records}


@app.get("/api/conversations")
def list_conversations():
    convs = list_active_conversations()
    return {"conversations": convs}


@app.post("/api/conversations/sync-all")
async def sync_all_boss_conversations(req: SyncConversationsRequest = SyncConversationsRequest()):
    """从 BOSS 聊天页同步当前账号的会话列表；全局自动回复开启时先处理未读消息。"""
    global monitor_paused
    if not automation or automation.page is None:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    lock = _browser_lock()
    if lock.locked():
        return {"success": False, "message": "浏览器正忙，请稍后再同步"}

    was_paused = monitor_paused
    monitor_paused = True
    try:
        async with lock:
            sync_result = await _run_pw(automation.sync_all_conversations, max(1, min(req.limit, 200)))
            monitor_result = None
    finally:
        monitor_paused = was_paused

    await broadcast_ws({"type": "new_messages", "summary": sync_result})
    return {"success": bool(sync_result.get("success", True)), "sync": sync_result, "monitor": monitor_result}


@app.get("/api/conversations/{conv_id}")
def get_conversation_detail(conv_id: int):
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = _clean_messages_for_web(get_messages(conv_id, 500))
    return {"conversation": conv, "messages": messages}


@app.get("/api/conversations/{conv_id}/messages")
def get_conversation_messages(conv_id: int, limit: int = 500):
    # 这个接口被前端频繁轮询，必须只读本地缓存，不能每次都控制浏览器。
    return {"messages": _clean_messages_for_web(get_messages(conv_id, limit))}


@app.post("/api/conversations/{conv_id}/sync")
async def sync_conversation_messages(conv_id: int):
    """按需从当前 BOSS 浏览器会话同步一次消息。"""
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not automation or automation.page is None:
        return {
            "success": False,
            "message": "浏览器未启动",
            "messages": _clean_messages_for_web(get_messages(conv_id, 500)),
        }

    hr_name = conv.get("hr_name", "")
    if not hr_name:
        raise HTTPException(status_code=400, detail="会话缺少HR姓名")

    lock = _browser_lock()
    if lock.locked():
        return {
            "success": False,
            "message": "浏览器正忙，先显示缓存",
            "messages": _clean_messages_for_web(get_messages(conv_id, 500)),
        }

    try:
        async with lock:
            opened = await asyncio.wait_for(_run_pw(automation.open_conversation_by_name, hr_name), timeout=8)
            if not opened:
                return {
                    "success": False,
                    "message": f"无法打开 {hr_name} 的会话",
                    "messages": _clean_messages_for_web(get_messages(conv_id, 500)),
                }

            live_messages = await asyncio.wait_for(_run_pw(automation.read_all_messages), timeout=20)
            if live_messages:
                replace_conversation_messages(conv_id, live_messages)
                last = live_messages[-1]
                update_conversation_last_message(conv_id, last.get("content", ""), last.get("sender", "hr"))
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": "同步超时，先显示缓存",
            "messages": _clean_messages_for_web(get_messages(conv_id, 500)),
        }

    return {
        "success": True,
        "messages": _clean_messages_for_web(get_messages(conv_id, 500)),
    }


@app.post("/api/conversations/{conv_id}/send")
async def send_manual_message(conv_id: int, req: SendMessageRequest):
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    hr_name = conv.get("hr_name", "")
    if not hr_name:
        raise HTTPException(status_code=400, detail="会话缺少HR姓名")
    lock = _browser_lock()
    if lock.locked():
        raise HTTPException(status_code=409, detail=_browser_busy_message("发送消息"))

    # 先打开对应会话
    async with lock:
        opened = await _run_pw(automation.open_conversation_by_name, hr_name)
        if not opened:
            raise HTTPException(status_code=500, detail=f"无法在浏览器中打开 {hr_name} 的会话")

        browser_ok = await _run_pw(automation.send_message, req.content, False)
    if not browser_ok:
        raise HTTPException(status_code=500, detail="浏览器发送失败，本地不会写入这条消息")

    add_message(conv_id, "me", req.content, ai_generated=False)
    update_conversation_last_message(conv_id, req.content, "me")
    await broadcast_ws(
        {
            "type": "manual_message_sent",
            "conversation_id": conv_id,
        }
    )
    return {"success": True, "browser_sent": browser_ok}


@app.post("/api/conversations/{conv_id}/auto-reply")
async def toggle_conversation_auto_reply(conv_id: int, req: AutoReplyToggleRequest):
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    set_auto_reply(conv_id, req.enabled)
    if req.enabled:
        update_conversation_status(conv_id, "active")
    await broadcast_ws(
        {
            "type": "auto_reply_toggled",
            "conversation_id": conv_id,
            "enabled": req.enabled,
        }
    )
    return {"status": "ok", "enabled": req.enabled}


@app.post("/api/conversations/{conv_id}/open")
async def open_conversation_in_browser(conv_id: int):
    """在浏览器中打开对应会话。"""
    if not automation:
        raise HTTPException(status_code=503, detail="浏览器未启动")
    conv = get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    hr_name = conv.get("hr_name", "")
    if not hr_name:
        raise HTTPException(status_code=400, detail="会话缺少HR姓名")
    lock = _browser_lock()
    if lock.locked():
        return {"success": False, "message": _browser_busy_message("打开会话")}
    async with lock:
        success = await _run_pw(automation.open_conversation_by_name, hr_name)
    return {
        "success": success,
        "message": f"已在浏览器中打开 {hr_name} 的会话" if success else "打开失败",
    }


@app.post("/api/conversations/{conv_id}/pause")
async def pause_auto_reply(conv_id: int):
    set_auto_reply(conv_id, False)
    await broadcast_ws(
        {
            "type": "auto_reply_toggled",
            "conversation_id": conv_id,
            "enabled": False,
        }
    )
    return {"status": "ok"}


@app.post("/api/conversations/{conv_id}/resume")
async def resume_auto_reply(conv_id: int):
    set_auto_reply(conv_id, True)
    update_conversation_status(conv_id, "active")
    await broadcast_ws(
        {
            "type": "auto_reply_toggled",
            "conversation_id": conv_id,
            "enabled": True,
        }
    )
    return {"status": "ok"}


# ══════════════════════════════════════
#  设置
# ══════════════════════════════════════


@app.get("/api/settings")
def read_settings():
    settings = get_all_settings()
    # 检查AI Key是否已配置
    ai_key = settings.get("ai_api_key", "")
    settings["ai_key_configured"] = "true" if ai_key and len(ai_key) > 10 else "false"
    return {"settings": settings}


@app.put("/api/settings")
async def update_settings(req: SettingsUpdate):
    updates = {}
    for k, v in req.model_dump().items():
        if k == "ai_api_key" and v:
            set_setting("ai_api_key", str(v))
            updates["ai_key_configured"] = "true"
            continue
        if v is not None and v != "":
            set_setting(k, str(v))
            updates[k] = str(v)
    await broadcast_ws({"type": "settings_updated", "updates": updates})
    return {"status": "ok", "updated": updates}


# ══════════════════════════════════════
#  WebSocket
# ══════════════════════════════════════


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "status": {
                    "browser_running": automation is not None,
                    "monitor_running": monitor_task is not None and not monitor_task.done(),
                },
            }
        )
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ══════════════════════════════════════
#  后台监控循环
# ══════════════════════════════════════


async def chat_monitor_loop():
    """后台轮询聊天消息 + 自动回复。带 session 心跳保活。"""
    global automation, monitor_paused
    await asyncio.sleep(3)  # 启动后简短等待

    if automation:
        print("[监控] 后台监控任务已启动")
        await _run_pw(automation.keep_alive)

    # 验证 AI 回复系统
    try:
        sys.path.insert(0, str(Path(__file__).parent / "interview"))
        from llm_client import _load_ai_config

        cfg = _load_ai_config()
        if cfg["api_key"] and len(cfg["api_key"]) > 10:
            print(f"[监控] AI API 已配置（{cfg['model']}），自动回复就绪")
        else:
            print("[监控] ⚠️ AI API Key 未配置，请在前端设置页配置")
    except Exception as e:
        print(f"[监控] ⚠️ AI 回复系统加载失败: {e}")

    # 首次立即跑一轮监控，不等延迟
    if automation:
        print("[监控] 执行首次会话扫描...")
        try:
            lock = _browser_lock()
            if lock.locked():
                print("  [监控] 浏览器正忙，跳过首次扫描")
                result = {}
            else:
                async with lock:
                    result = await _run_pw(automation.run_chat_monitor_cycle)
            if result.get("new_messages", 0) > 0:
                await broadcast_ws({"type": "new_messages", "summary": result})
            if result.get("replies_sent", 0) > 0:
                await broadcast_ws({"type": "auto_reply_sent", "summary": result})
            if result.get("new_conversations"):
                await broadcast_ws({"type": "new_messages"})
        except Exception as e:
            print(f"  [监控] 首次扫描异常: {e}")

    _heartbeat_count = 0
    _heartbeat_misses = 0
    while True:
        try:
            min_delay = int(get_setting("min_reply_delay_sec", "15"))
            max_delay = int(get_setting("max_reply_delay_sec", "20"))
            delay = random.randint(min(min_delay, max_delay), max(min_delay, max_delay) + 5)
            await asyncio.sleep(delay)

            if monitor_paused:
                continue

            if not automation:
                continue

            lock = _browser_lock()
            if lock.locked():
                continue

            async with lock:
                # 每轮轻量检查登录态（不导航，不触发BOSS反爬）
                _heartbeat_count += 1
                alive = await _run_pw(automation.heartbeat)
                if not alive:
                    await asyncio.sleep(5)
                    alive = await _run_pw(automation.heartbeat)

                if not alive:
                    _heartbeat_misses += 1
                else:
                    _heartbeat_misses = 0

                if _heartbeat_misses >= 2:
                    monitor_paused = True
                    await broadcast_ws(
                        {
                            "type": "session_expired",
                            "message": "BOSS直聘登录已过期，请点击设置Tab的「重新扫码登录」",
                        }
                    )
                    await broadcast_ws({"type": "monitor_paused"})
                    continue

                # 每轮都轻量保活，避免 BOSS session 超时
                if _heartbeat_count >= 1:
                    await _run_pw(automation.keep_alive)

                if get_setting("auto_reply_enabled", "false") != "true":
                    continue

                result = await _run_pw(automation.run_chat_monitor_cycle)

                if result.get("new_messages", 0) > 0:
                    await broadcast_ws(
                        {
                            "type": "new_messages",
                            "summary": result,
                        }
                    )
                if result.get("replies_sent", 0) > 0:
                    await broadcast_ws(
                        {
                            "type": "auto_reply_sent",
                            "summary": result,
                        }
                    )
                if result.get("new_conversations"):
                    await broadcast_ws({"type": "new_messages"})
                if result.get("wechat_exchanged"):
                    await broadcast_ws({"type": "wechat_exchanged"})

                safety_ok = await _run_pw(automation.check_page_safety)
                if not safety_ok:
                    monitor_paused = True
                    await broadcast_ws(
                        {
                            "type": "safety_warning",
                            "message": "检测到页面异常(验证码/登录失效/账号限制)，已暂停自动操作。请手动检查浏览器。",
                        }
                    )
                    await broadcast_ws({"type": "monitor_paused"})
                    continue

        except asyncio.CancelledError:
            break
        except Exception as e:
            await broadcast_ws(
                {
                    "type": "error",
                    "message": f"监控循环异常: {e}",
                }
            )
            await asyncio.sleep(60)


# ══════════════════════════════════════
#  启动
# ══════════════════════════════════════


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--auto-start", action="store_true", help="启动时自动打开浏览器")
    args = parser.parse_args()

    if args.auto_start:
        global automation, monitor_task
        try:

            def _do_start():
                a = BossAutomation(headless=False)
                a.start()
                return a

            automation = _playwright_executor.submit(_do_start).result()
            print("✅ 浏览器已启动")
        except Exception as e:
            print(f"⚠️ 自动启动失败: {e}")

    print(f"\n🚀 BOSS直聘自动化控制台: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
