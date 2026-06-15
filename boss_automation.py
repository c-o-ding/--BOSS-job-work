#!/usr/bin/env python3
"""
BossAutomation — 继承 BossScraper，增加点击/输入/聊天等交互能力。
"""

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeoutError

from boss_firefox import BossScraper, pause, decode_salary
from boss_state import (
    init_db,
    add_application,
    get_application_by_url,
    update_application_status,
    get_setting,
    get_today_application_count,
    get_or_create_conversation,
    get_conversation,
    add_message,
    get_messages,
    get_recent_messages,
    replace_conversation_messages,
    message_exists,
    update_conversation_last_message,
    update_conversation_status,
    update_conversation_interest,
    update_conversation_wechat,
    mark_resume_sent,
    mark_auto_reply_signature,
    increment_daily_stat,
    get_today_auto_reply_count,
    find_conversation_by_hr_name,
    get_daily_stats,
)

# ── 选择器配置（BOSS UI 改版时只改这里，也可通过设置表覆盖）──
SELECTORS = {
    "apply_button": [
        'button:has-text("立即沟通")',
        'a:has-text("立即沟通")',
        'button:has-text("继续沟通")',
        'a:has-text("继续沟通")',
        '[class*="btn-chat"]',
        '[class*="start-chat"]',
        'span:has-text("立即沟通")',
        'span:has-text("继续沟通")',
        'div:has-text("立即沟通")',
        'div:has-text("继续沟通")',
    ],
    "chat_input": [
        "#chat-input",
        'textarea[placeholder*="简短"]',
        'textarea[placeholder*="问题"]',
        'textarea[placeholder*="描述"]',
        'textarea',
        'div[contenteditable="true"]',
        '[class*="chat-input"]',
        '[class*="input"] textarea',
        '[placeholder*="请输入"]',
        '[placeholder*="简短描述"]',
    ],
    "chat_send_button": [
        'button[type="send"]',
        ".btn-send",
        ".btn-sure",
        ".btn-primary:has-text(\"发送\")",
        'button:has-text("发送")',
        'a:has-text("发送")',
        'span:has-text("发送")',
        'button[class*="send"]',
    ],
    "conversation_items": [
        'li[role="listitem"]',
        ".friend-content",
        '[class*="chat-item"]',
    ],
    "message_items_in_chat": [
        "li.message-item",
        'li[class*="message-item"]',
        '[class*="message-item"]',
    ],
    "unread_badge": [
        '[class*="unread"]',
        '[class*="badge"]',
        ".red-dot",
    ],
    "greeting_dialog_close": [
        'button[class*="close"]',
        '[class*="dialog-close"]',
        'span:has-text("×")',
        '[class*="modal-close"]',
        'svg[class*="close"]',
    ],
    "resume_attach_btn": [
        'div.toolbar-btn:has-text("发简历")',
        'div:has-text("发简历")',
        'button:has-text("发简历")',
        'span:has-text("发简历")',
    ],
    "resume_confirm_btn": [
        ".btn-sure-v2.btn-confirm",
        ".choose-resume-dialog .btn-confirm",
        'button:has-text("发送")',
        '.boss-popup__content button:has-text("发送")',
    ],
    "wechat_share_btn": [
        ".btn-weixin",
        'div:has-text("换微信")',
        'span:has-text("换微信")',
        '[class*="btn-weixin"]',
    ],
    "phone_share_btn": [
        ".btn-contact",
        'div:has-text("换电话")',
        'span:has-text("换电话")',
        '[class*="btn-contact"]',
    ],
    "back_to_list": [
        '[class*="back"]',
        'span:has-text("返回")',
        'button:has-text("返回")',
        'a[href*="/chat"]',
    ],
}


def _merge_selectors():
    """合并 settings 表中的选择器覆盖。"""
    try:
        from boss_state import get_setting
        import json as _json

        raw = get_setting("selector_overrides", "")
        if raw:
            overrides = _json.loads(raw)
            for k, v in overrides.items():
                if k in SELECTORS and isinstance(v, list) and len(v) > 0:
                    SELECTORS[k] = v
    except Exception:
        pass


_merge_selectors()


MONITOR_LOG_PATH = Path(__file__).parent / ".boss_profile" / "monitor.log"


def monitor_log(message: str):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line)
    try:
        MONITOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MONITOR_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 绝对上限：防止误操作无限投递；实际日限仍以设置里的 daily_apply_limit 为准。
MAX_APPLY_PER_DAY = 300
MAX_AUTO_REPLY_PER_DAY = 200


class BossAutomation(BossScraper):
    """在 BossScraper 基础上增加交互能力"""

    def __init__(self, headless=False):
        super().__init__(headless)
        init_db()

    # ══════════════════════════════════════
    #  底层交互 helpers
    # ══════════════════════════════════════

    def _find_element(self, selector_list: List[str], timeout_ms: int = 5000) -> Optional[Locator]:
        """逐个尝试选择器，返回第一个可见匹配。"""
        self._ensure_active_page()
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for sel in selector_list:
                try:
                    loc = self.page.locator(sel).first
                    if loc.is_visible():
                        return loc
                except Exception:
                    continue
            time.sleep(0.3)
        return None

    def _find_all_elements(self, selector_list: List[str]) -> List[Locator]:
        """返回所有匹配的可见元素。"""
        for sel in selector_list:
            try:
                locs = self.page.locator(sel)
                count = locs.count()
                if count > 0:
                    return [locs.nth(i) for i in range(count)]
            except Exception:
                continue
        return []

    def _human_type(self, locator: Locator, text: str):
        """逐字输入，模拟真人打字。"""
        try:
            locator.click()
            time.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass
        for ch in text:
            self.page.keyboard.type(ch, delay=random.randint(50, 150))
        time.sleep(random.uniform(0.3, 0.8))

    def _safe_click(self, locator: Locator):
        """带随机延迟的点击。"""
        time.sleep(random.uniform(0.2, 0.6))
        try:
            locator.hover()
            time.sleep(random.uniform(0.1, 0.3))
        except Exception:
            pass
        last_error = None
        for kwargs in (
            {"timeout": 8000, "no_wait_after": True},
            {"timeout": 5000, "force": True, "no_wait_after": True},
            {"timeout": 5000, "force": True},
        ):
            try:
                locator.click(**kwargs)
                clicked = True
                break
            except PlaywrightTimeoutError as e:
                last_error = e
                clicked = False
                print(f"  ⚠️ 点击超时，尝试兜底点击: {str(e).splitlines()[0]}")
            except TypeError:
                # 兼容不同 Playwright 版本的 click 参数。
                try:
                    locator.click(timeout=kwargs.get("timeout", 5000), force=kwargs.get("force", False))
                    clicked = True
                    break
                except Exception as e:
                    last_error = e
                    clicked = False
            except Exception as e:
                last_error = e
                clicked = False

        if not clicked:
            try:
                locator.evaluate(
                    """el => {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        el.click();
                        return true;
                    }"""
                )
                clicked = True
                print("  [点击] 已使用 DOM 兜底点击")
            except Exception as e:
                raise last_error or e
        try:
            self._close_extra_pages()
        except Exception:
            pass
        return clicked

    def _has_text(self, *texts: str) -> bool:
        """检查页面是否包含任意关键词。"""
        try:
            body = self.page.inner_text("body").lower()
            return any(t.lower() in body for t in texts)
        except Exception:
            return False

    def _read_input_text(self, locator: Locator) -> str:
        try:
            return locator.evaluate(
                """el => {
                    if ('value' in el) return el.value || '';
                    return el.innerText || el.textContent || '';
                }"""
            ).strip()
        except Exception:
            return ""

    def _wait_for_chat_input(self, timeout_ms: int = 15000) -> Optional[Locator]:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 10000))
        except Exception:
            pass
        return self._find_element(SELECTORS["chat_input"], timeout_ms=timeout_ms)

    def _wait_input_cleared(self, locator: Locator, timeout_sec: float = 3.0) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if not self._read_input_text(locator):
                return True
            time.sleep(0.2)
        return False

    # ══════════════════════════════════════
    #  安全检查
    # ══════════════════════════════════════

    def check_page_safety(self) -> bool:
        """所有自动化操作前检查页面安全状态。"""
        try:
            self._ensure_active_page()
            url = self.page.url
            body = self.page.inner_text("body")
            body_lower = body.lower()

            if self._login_prompt_visible():
                print("  ⚠️ 安全检查: 需要重新登录")
                return False
            if "/web/geek/chat" in url and any(
                marker in body
                for marker in ("搜索30天内的联系人", "全部", "未读", "新招呼", "仅沟通", "发简历", "换电话", "换微信")
            ):
                return True
            if any(kw in body_lower[:500] for kw in ["验证", "滑块", "拼图", "captcha", "verify"]):
                print("  ⚠️ 安全检查: 检测到验证码")
                return False
            if any(kw in body_lower[:500] for kw in ["账号异常", "违规", "限制使用", "冻结"]):
                print("  ⚠️ 安全检查: 账号异常")
                return False
            if any(kw in body_lower[:500] for kw in ["操作太频繁", "稍后再试", "休息一下"]):
                print("  ⚠️ 安全检查: 操作频率限制")
                return False
            return True
        except Exception:
            return True

    # ══════════════════════════════════════
    #  Session 保活 & 心跳
    # ══════════════════════════════════════

    def check_logged_in(self) -> bool:
        """快速检查当前是否已登录；未知空白页不直接当作过期。"""
        try:
            return self.is_logged_in_page()
        except Exception:
            return False

    def heartbeat(self) -> bool:
        """心跳: 只检查当前页面登录状态，不主动跳转。"""
        try:
            return self.check_logged_in()
        except Exception:
            return False

    def keep_alive(self):
        """主动保活: 在聊天页保持 BOSS session 活跃。已登录时用轻量操作代替完整刷新。"""
        try:
            self._ensure_active_page()
            current_url = self.page.url
            need_navigate = "/web/geek/chat" not in current_url
            try:
                if need_navigate:
                    self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=30000)
                    pause(2, 4)
                else:
                    # 已在聊天页，轻量滚动模拟用户活动，避免频繁 reload 被检测
                    try:
                        self.page.mouse.move(random.randint(200, 600), random.randint(300, 500))
                        pause(0.5, 1.0)
                        self.page.evaluate("window.scrollBy(0, %d)" % random.randint(-100, 100))
                    except Exception:
                        pass
            except Exception:
                pass
            return self.check_logged_in()
        except Exception:
            return False

    def _save_state(self):
        """保存当前浏览器状态到文件。"""
        try:
            from boss_firefox import STATE_FILE

            state = self._ctx.storage_state()
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception:
            pass

    # ══════════════════════════════════════
    #  自动投递
    # ══════════════════════════════════════

    def apply_to_job(self, job_url: str, greeting: Optional[str] = None) -> dict:
        """
        对单个岗位执行投递流程:
        1. 打开详情页
        2. 点击"立即沟通"
        3. 发送招呼语
        返回 {success, message, application_id}
        """
        if not job_url:
            return {"success": False, "message": "缺少岗位链接"}

        # 日限检查
        today_count = get_today_application_count()
        try:
            daily_limit = max(1, int(get_setting("daily_apply_limit", "15") or "15"))
        except (TypeError, ValueError):
            daily_limit = 15
        if today_count >= min(daily_limit, MAX_APPLY_PER_DAY):
            return {"success": False, "message": f"已达今日上限({today_count}条)"}

        print(f"  🚀 投递: {job_url[:60]}...")

        try:
            self._ensure_active_page()
            self.page.goto(job_url, wait_until="load", timeout=45000)
            pause(1, 2)

            if not self.check_page_safety():
                return {"success": False, "message": "安全检查未通过"}

            existing_before = get_application_by_url(job_url)
            if self._has_text("已沟通", "继续沟通") and existing_before and existing_before.get("greeting_sent_at"):
                if existing_before["status"] == "pending":
                    update_application_status(existing_before["id"], "applied")
                return {
                    "success": True,
                    "message": "已沟通过且已记录招呼语",
                    "already_applied": True,
                    "application_id": existing_before["id"],
                    "greeting_sent": True,
                }

            # 查找"立即沟通"按钮
            apply_btn = self._find_element(SELECTORS["apply_button"])
            if not apply_btn:
                try:
                    apply_btn = self.page.locator("text=立即沟通").first
                    if not apply_btn.is_visible():
                        apply_btn = self.page.locator("text=继续沟通").first
                        if not apply_btn.is_visible():
                            apply_btn = None
                except Exception:
                    apply_btn = None

            if not apply_btn:
                return {"success": False, "message": "未找到投递按钮"}

            self._safe_click(apply_btn)
            pause(2, 3)

            # 检查限制消息
            if self._has_text("已达上限", "沟通人数已用完", "今日次数已用完", "今日沟通次数已用完"):
                return {"success": False, "message": "BOSS直聘今日沟通次数已用完"}

            # 等待聊天窗口加载。BOSS 经常先跳到聊天页，再延迟渲染输入框。
            chat_input = self._wait_for_chat_input(timeout_ms=15000)

            # 发送招呼语
            greeting_text = greeting or get_setting(
                "greeting_template",
                "您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？",
            )
            greeting_sent = False
            if chat_input and greeting_text:
                greeting_sent = self.send_message(greeting_text)
                if greeting_sent:
                    print(f"  ✅ 招呼语已发送")
                else:
                    print(f"  ⚠️ 招呼语发送失败")
            elif greeting_text:
                print("  ⚠️ 未找到聊天输入框，招呼语未发送")

            # 记录到 SQLite
            existing = get_application_by_url(job_url)
            if existing:
                if greeting_sent:
                    update_application_status(existing["id"], "applied", greeting_text)
                else:
                    update_application_status(existing["id"], "applied")
                app_id = existing["id"]
            else:
                app_id = add_application({"title": "", "company": "", "url": job_url})
                if greeting_sent:
                    update_application_status(app_id, "applied", greeting_text)
                else:
                    update_application_status(app_id, "applied")

            # 从详情页提取 HR 真实姓名和岗位信息
            hr_name = ""
            hr_company = ""
            job_title = ""
            try:
                from boss_firefox import BossScraper

                hr_info = self.page.evaluate("""() => {
                    const body = (document.body || {}).innerText || '';
                    const lines = body.split('\\n').map(l => l.trim()).filter(Boolean);
                    let hrName = '', hrTitle = '';
                    for (let i = 0; i < lines.length; i++) {
                        const l = lines[i];
                        if (l.includes('HR') || l.includes('招聘者') || l.includes('招聘经理') ||
                            l.includes('人事') || l.includes('HRBP') || l.includes('猎头')) {
                            if (i > 0 && lines[i-1].length <= 6 && !/\\d|省|市|区|路|号|招聘|公司|BOSS/.test(lines[i-1])) {
                                hrName = lines[i-1];
                            }
                            hrTitle = l;
                            break;
                        }
                    }
                    return {hrName, hrTitle};
                }""")
                hr_name = (hr_info.get("hrName") or "").strip()
                if not hr_name:
                    hr_name = ""
            except Exception:
                pass

            app_record = get_application_by_url(job_url) or {}
            hr_name = hr_name or app_record.get("hr_name", "")
            hr_company = app_record.get("company", "")
            job_title = app_record.get("job_title", "")

            # 只创建有 HR 名字的会话，避免"未知HR"垃圾数据
            if hr_name and len(hr_name) >= 2:
                get_or_create_conversation(app_id, hr_name, hr_company, job_title)

            increment_daily_stat("applications_sent")
            msg = "投递成功，招呼语已发送" if greeting_sent else "已打开沟通，但招呼语未确认发送"
            print(f"  ✅ {msg}")
            return {"success": True, "message": msg, "application_id": app_id, "greeting_sent": greeting_sent}

        except Exception as e:
            print(f"  ❌ 投递失败: {e}")
            return {"success": False, "message": str(e)}

    def apply_batch(self, job_urls: List[str], greeting_template: Optional[str] = None) -> List[dict]:
        """批量投递，带间隔延迟。可通过设置 batch_delay_sec 控制间隔。"""
        results = []
        min_delay = int(get_setting("batch_delay_min_sec", "30"))
        max_delay = int(get_setting("batch_delay_max_sec", "90"))
        for i, url in enumerate(job_urls):
            if i > 0:
                delay = random.uniform(min_delay, max_delay)
                print(f"  ⏳ 等待 {delay:.0f}s 后投递下一条...")
                time.sleep(delay)

            result = self.apply_to_job(url, greeting_template)
            results.append(result)

            if not result["success"] and "上限" in result.get("message", ""):
                break
        return results

    # ══════════════════════════════════════
    #  聊天监控
    # ══════════════════════════════════════

    def navigate_to_chat(self) -> bool:
        """导航到 BOSS 聊天页，切到「未读」标签，只显示有未读消息的会话。"""
        try:
            self._ensure_active_page()
            self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=45000)
            pause(2, 3)
            # 点击「未读」标签，只显示有未读的会话
            for sel in ['span.label-name:has-text("未读")', 'li:has-text("未读")', '.label-name:has-text("未读")']:
                try:
                    unread_tab = self.page.locator(sel).first
                    if unread_tab.is_visible():
                        unread_tab.click()
                        pause(1, 2)
                        break
                except Exception:
                    pass
            return self.check_page_safety()
        except Exception:
            return False

    def poll_conversation_list(self) -> List[dict]:
        """从 BOSS 聊天页 DOM 获取会话列表。DOM 失败用 body text 正则兜底。"""
        conversations = []

        # 方式1: DOM 选择器
        conv_els = self._find_all_elements(SELECTORS["conversation_items"])
        if conv_els:
            for el in conv_els:
                try:
                    text = el.inner_text().strip()
                    if not text or len(text) < 3:
                        continue
                    # 从 BOSS 真实结构提取 HR 名字: .name-text
                    try:
                        hr_name = el.locator(".name-text").first.inner_text().strip()
                    except Exception:
                        hr_name = ""
                    if not hr_name:
                        # 兜底：从 body_text 行中提取
                        hr_name = (
                            el.evaluate("""(el) => {
                            const lines = (el.innerText||'').split('\\n').map(l=>l.trim()).filter(Boolean);
                            for (const l of lines) {
                                if (/^\\d{1,2}:\\d{2}$/.test(l)) continue;
                                if (/^\\[.+\\]$/.test(l)) continue;
                                const ch = l.replace(/[^\\u4e00-\\u9fff]/g,'');
                                if (ch.length>=2 && ch.length<=5) return l.split(/[\\s|·]/)[0].trim();
                            }
                            return '';
                        }""")
                            or ""
                        )
                    has_unread = False
                    try:
                        badge = el.locator('.red-dot, [class*="unread"]').first
                        has_unread = badge.is_visible()
                    except Exception:
                        pass
                    conversations.append(
                        {
                            "text": text,
                            "has_unread": has_unread,
                            "element": el,
                            "hr_name": hr_name,
                        }
                    )
                except Exception:
                    continue

        # 方式2: body text 正则兜底
        if not conversations:
            try:
                body = self.page.inner_text("body") or ""
                pattern = r"(\d{1,2}:\d{2})\s+([\u4e00-\u9fff\w·]+?)\s+(\[\s*\S+\s*\])\s+(.+?)(?=\s*\d{1,2}:\d{2}\s+|没有更多了|\Z)"
                for m in re.findall(pattern, body):
                    time_str, name_block, status, msg = m
                    # 提取纯名字：从 name_block 中去掉公司后缀
                    hr_name = re.sub(
                        r"[\u4e00-\u9fff]{2,}(?:有限|集团|科技|网络|信息|文化|教育|医疗|能源|贸易|实业|发展|控股|投资).*|经理.*|主管.*|专员.*|总监.*|[\[\]].*",
                        "",
                        name_block,
                    ).strip()
                    if not hr_name or len(hr_name) < 2:
                        m2 = re.match(r"^[\u4e00-\u9fff]{2,4}", name_block)
                        hr_name = m2.group(0) if m2 else name_block[:6]
                    hr_name = hr_name.strip()
                    if not hr_name or len(hr_name) < 2:
                        continue
                    conversations.append(
                        {
                            "text": f"{time_str}\n{name_block}\n{status}\n{msg}".strip(),
                            "has_unread": "未读" in status,
                            "element": None,
                            "hr_name": hr_name,
                        }
                    )
            except Exception:
                pass

        return conversations

    def _click_chat_label(self, label: str) -> bool:
        try:
            clicked = self.page.evaluate(
                """(label) => {
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const candidates = [];
                document.querySelectorAll('span, li, button, a, div').forEach(el => {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (text !== label) return;
                    if (!visible(el)) return;
                    const r = el.getBoundingClientRect();
                    if (r.top > window.innerHeight * 0.45) return;
                    candidates.push({el, area: r.width * r.height, top: r.top, left: r.left});
                });
                candidates.sort((a, b) => a.area - b.area || a.top - b.top || a.left - b.left);
                if (!candidates.length) return false;
                const el = candidates[0].el;
                el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                el.click();
                return true;
            }""",
                label,
            )
            if clicked:
                pause(0.5, 1)
                return True
        except Exception:
            pass
        for sel in [
            f'span.label-name:has-text("{label}")',
            f'.label-name:has-text("{label}")',
            f'li:has-text("{label}")',
            f'text="{label}"',
        ]:
            try:
                tab = self.page.locator(sel).first
                if tab.count() > 0 and tab.is_visible():
                    tab.click()
                    pause(0.5, 1)
                    return True
            except Exception:
                pass
        return False

    def _is_valid_conversation_name(self, hr_name: str) -> bool:
        hr_name = (hr_name or "").strip()
        skip_keywords = [
            "消息",
            "联系人",
            "沟通",
            "设置",
            "搜索",
            "我的",
            "首页",
            "已沟通",
            "继续沟通",
            "新对话",
            "系统",
            "通知",
            "BOSS",
            "在线",
            "离线",
            "刚刚",
            "分钟",
            "小时",
            "昨天",
            "简历",
            "附件",
            "上传",
            "制作",
            "更新",
            "AI",
        ]
        return (
            bool(hr_name)
            and len(hr_name) >= 2
            and not hr_name.isdigit()
            and not any(kw == hr_name for kw in skip_keywords)
            and not any(kw in hr_name and len(hr_name) <= len(kw) + 1 for kw in skip_keywords)
        )

    def _guess_conversation_meta(self, text: str, hr_name: str) -> dict:
        lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
        joined = " ".join(lines[:4])
        clean = re.sub(r"\d{1,2}:\d{2}|\[.*?\]|送达|已读|未读|刚刚|昨天|前天", " ", joined)
        if hr_name:
            clean = clean.replace(hr_name, " ", 1)
        company = ""
        job_title = ""
        company_match = re.search(
            r"[\u4e00-\u9fa5A-Za-z0-9·（）()]{3,30}(?:公司|科技|集团|网络|信息|智能|互联网|咨询|有限责任公司|有限公司)",
            clean,
        )
        if company_match:
            company = company_match.group(0).strip()
        for line in lines[:5]:
            if any(kw in line for kw in ("工程师", "产品", "运营", "销售", "顾问", "算法", "开发", "测试", "设计")):
                job_title = re.sub(r"\d{1,2}:\d{2}|\[.*?\]|送达|已读|未读", "", line).strip()
                if hr_name:
                    job_title = job_title.replace(hr_name, "", 1).strip()
                if company:
                    job_title = job_title.replace(company, "", 1).strip()
                break
        return {"company": company[:60], "job_title": job_title[:100]}

    def _scroll_conversation_list(self) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    """() => {
                    const selectors = [
                        '.user-list', '[class*="user-list"]', '[class*="friend-list"]',
                        '[class*="conversation-list"]', '[class*="chat-list"]'
                    ];
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    for (const sel of selectors) {
                        for (const el of document.querySelectorAll(sel)) {
                            if (!visible(el) || el.scrollHeight <= el.clientHeight + 10) continue;
                            const before = el.scrollTop;
                            el.scrollTop = Math.min(el.scrollTop + Math.max(240, el.clientHeight * 0.85), el.scrollHeight);
                            return el.scrollTop !== before;
                        }
                    }
                    window.scrollBy(0, 500);
                    return true;
                }"""
                )
            )
        except Exception:
            try:
                self.page.mouse.wheel(0, 600)
                return True
            except Exception:
                return False

    def _reset_conversation_list_scroll(self):
        try:
            self.page.evaluate(
                """() => {
                const selectors = [
                    '.user-list', '[class*="user-list"]', '[class*="friend-list"]',
                    '[class*="conversation-list"]', '[class*="chat-list"]'
                ];
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (!visible(el) || el.scrollHeight <= el.clientHeight + 10) continue;
                        el.scrollTop = 0;
                        return true;
                    }
                }
                return false;
            }"""
            )
        except Exception:
            pass

    def _chat_page_ready(self) -> bool:
        try:
            if "/web/geek/chat" not in (self.page.url or ""):
                return False
            body = self.page.inner_text("body") or ""
            chat_markers = (
                "搜索30天内的联系人",
                "全部",
                "未读",
                "新招呼",
                "仅沟通",
                "发简历",
                "换电话",
                "换微信",
                "与您进行过沟通",
            )
            return any(marker in body for marker in chat_markers) and not self._login_prompt_visible()
        except Exception:
            return False

    def _current_page_hint(self) -> str:
        try:
            body = (self.page.inner_text("body") or "").replace("\n", " | ")
            return f"url={self.page.url}; body={body[:180]}"
        except Exception:
            return ""

    def sync_all_conversations(self, limit: int = 50) -> dict:
        """同步 BOSS 聊天页加载出来的全部会话到本地会话列表。"""
        from boss_state import list_active_conversations

        result = {"success": False, "checked": 0, "synced": 0, "created": 0, "messages": 0}
        try:
            if "/web/geek/chat" not in self.page.url:
                self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=45000)
                pause(2, 3)
            else:
                pause(0.5, 1)

            self._click_chat_label("全部")
            pause(0.5, 1)
            self._reset_conversation_list_scroll()
            if not self._chat_page_ready() and not self.check_page_safety():
                result["message"] = "当前BOSS聊天页不可用或登录态未被后端识别"
                result["page_hint"] = self._current_page_hint()
                return result

            candidates = []
            seen = set()
            max_scrolls = min(20, max(3, limit // 8 + 2))
            for _ in range(max_scrolls):
                for conv in self.poll_conversation_list():
                    hr_name = (conv.get("hr_name") or "").strip()
                    if not self._is_valid_conversation_name(hr_name):
                        continue
                    key = hr_name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    meta = self._guess_conversation_meta(conv.get("text", ""), hr_name)
                    candidates.append(
                        {
                            "hr_name": hr_name,
                            "company": conv.get("company") or meta["company"],
                            "job_title": conv.get("job_title") or meta["job_title"],
                            "has_unread": conv.get("has_unread", False),
                        }
                    )
                    if len(candidates) >= limit:
                        break
                if len(candidates) >= limit:
                    break
                if not self._scroll_conversation_list():
                    break
                pause(0.4, 0.8)

            result["checked"] = len(candidates)
            known_before = {c.get("hr_name") for c in list_active_conversations()}

            for conv in candidates[:limit]:
                hr_name = conv["hr_name"]
                conv_id = get_or_create_conversation(None, hr_name, conv.get("company", ""), conv.get("job_title", ""))
                if hr_name not in known_before:
                    result["created"] += 1
                    known_before.add(hr_name)

                opened = self.open_conversation_by_name(hr_name)
                if not opened and len(hr_name) > 4:
                    short = re.match(r"^[\u4e00-\u9fff]{2,3}", hr_name)
                    if short:
                        opened = self.open_conversation_by_name(short.group(0))
                if not opened:
                    continue

                pause(0.8, 1.5)
                raw_msgs = self.read_all_messages()
                clean_msgs = []
                for msg in raw_msgs:
                    content = (msg.get("content") or "").strip()
                    if content:
                        clean_msgs.append(
                            {
                                "sender": msg.get("sender", "hr"),
                                "content": content,
                                "status": msg.get("status", ""),
                            }
                        )
                if clean_msgs:
                    replace_conversation_messages(conv_id, clean_msgs)
                    last = clean_msgs[-1]
                    update_conversation_last_message(conv_id, last["content"], last["sender"], 0)
                    result["messages"] += len(clean_msgs)
                result["synced"] += 1

            if not result["checked"]:
                result["success"] = False
                result["message"] = "已打开BOSS聊天页，但没有从左侧会话列表识别到会话"
                result["page_hint"] = self._current_page_hint()
            else:
                result["success"] = True
            return result
        except Exception as e:
            result["message"] = str(e)
            return result

    def read_visible_messages(self) -> List[dict]:
        """读取当前右侧聊天窗口中的可见消息，避免把左侧会话列表误当聊天内容。"""
        try:
            raw = self.page.evaluate("""() => {
                const result = [];
                const vw = window.innerWidth || 1200;
                const vh = window.innerHeight || 800;
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const inputTops = Array.from(document.querySelectorAll('textarea, input, [contenteditable="true"]'))
                    .filter(visible)
                    .map(el => el.getBoundingClientRect().top)
                    .filter(top => top > vh * 0.45)
                    .sort((a, b) => a - b);
                const inputTop = inputTops[0] || (vh - 90);
                const topLimit = Math.min(Math.max(190, vh * 0.28), 300);
                const inMessageBand = r => {
                    const cx = r.left + r.width / 2;
                    return cx > vw * 0.34 && r.left < vw * 0.98 && r.top > topLimit && r.bottom < inputTop - 8;
                };
                const clean = text => (text || '')
                    .replace(/^(已读|未读|送达|发送失败|已发送)\\s*/g, '')
                    .replace(/\\n?(已读|未读|送达|发送失败|已发送)$/g, '')
                    .replace(/\\n+/g, '\\n')
                    .trim();
                const pickStatus = text => {
                    const m = (text || '').match(/(^|\\n)\\s*(已读|未读|送达|发送失败|已发送)\\s*(\\n|$)/);
                    return m ? m[2] : '';
                };
                const badText = text => {
                    if (!text) return true;
                    if (/^(已读|未读|送达|发送失败|已发送|发送|发简历|换电话|换微信|更多|查看职位|预览|关注)$/.test(text)) return true;
                    if (/^\\d+\\s*[-~–—－]\\s*\\d+\\s*K(\\s*[·*]\\s*\\d+\\s*薪)?$/i.test(text)) return true;
                    if (/^\\d+\\s*[-~–—－]\\s*\\d+\\s*元\\s*\\/\\s*天$/i.test(text)) return true;
                    if (/^(北京|上海|深圳|广州|杭州|武汉|成都|南京|苏州|西安|天津|重庆)$/.test(text)) return true;
                    if (/搜索30天内的联系人|与您进行过沟通的 Boss 都会在左侧列表中显示/.test(text)) return true;
                    if (/请选择需要投递的简历|已上传附件|管理附件/.test(text)) return true;
                    if (/按Enter键发送|按Ctrl\\+Enter键换行|点击预览附件简历/.test(text)) return true;
                    if (/附件简历请求已发送|对方已同意|对方已查看了您的附件简历/.test(text)) return true;
                    return false;
                };
                const classTrail = el => {
                    const parts = [];
                    let cur = el;
                    for (let i = 0; cur && i < 4; i += 1, cur = cur.parentElement) {
                        parts.push(String(cur.className || ''));
                    }
                    return parts.join(' ');
                };
                const pickContent = el => {
                    const full = clean(el.innerText || el.textContent || '');
                    const cardLike = /附件简历|简历请求|是否同意|是否接受此工作地点|工作地点|可以接受|暂不考虑/.test(full);
                    if (cardLike && !badText(full) && full.length <= 1500 && full.split('\\n').length <= 18) {
                        return {node: el, text: full};
                    }
                    const selectors = [
                        '.text p',
                        '.text span:last-child',
                        '[class*="bubble"]',
                        '[class*="message-content"]',
                        '[class*="content"] p',
                        '[class*="content"] span',
                        '.text'
                    ];
                    for (const sel of selectors) {
                        const node = el.matches && el.matches(sel) ? el : el.querySelector(sel);
                        if (!node || !visible(node)) continue;
                        const text = clean(node.innerText || node.textContent || '');
                        if (!badText(text) && text.length <= 1000 && text.split('\\n').length <= 8) return {node, text};
                    }
                    const text = clean(el.innerText || el.textContent || '');
                    if (!badText(text) && text.length <= 1000 && text.split('\\n').length <= 8) return {node: el, text};
                    return null;
                };
                const push = (el, contentEl) => {
                    if (!visible(el)) return;
                    const r = el.getBoundingClientRect();
                    if (!inMessageBand(r)) return;
                    const picked = contentEl ? {node: contentEl, text: clean(contentEl.innerText || contentEl.textContent || '')} : pickContent(el);
                    if (!picked || badText(picked.text)) return;
                    const fullText = el.innerText || '';
                    const content = picked.text;
                    const isCardLike = /附件简历|简历请求|是否同意|是否接受此工作地点|工作地点|可以接受|暂不考虑/.test(content);
                    if (content.length > (isCardLike ? 1500 : 1000)) return;
                    if (content.split('\\n').length > (isCardLike ? 18 : 8)) return;
                    const cls = classTrail(el);
                    const cr = picked.node.getBoundingClientRect();
                    const sender = /item-myself|myself|self|mine|right/.test(cls)
                        || cr.left > vw * 0.55
                        || (cr.left > vw * 0.43 && cr.right > vw * 0.82)
                        ? 'me' : 'hr';
                    const status = sender === 'me' ? pickStatus(fullText) : '';
                    result.push({sender: sender, content: content, status: status, top: r.top, left: r.left});
                };

                document.querySelectorAll('li.message-item, li[class*="message-item"], [class*="message-item"], [class*="item-myself"], [class*="item-friend"], [class*="msg-item"]').forEach(el => push(el));
                if (result.length === 0) {
                    document.querySelectorAll('[class*="message"] [class*="bubble"], [class*="msg"] [class*="bubble"], [class*="chat"] [class*="text"], [class*="message"] .text, [class*="msg"] .text').forEach(el => push(el, el));
                }
                if (result.length === 0) {
                    const candidates = [];
                    document.querySelectorAll('p, span, div').forEach(el => {
                        if (!visible(el)) return;
                        const r = el.getBoundingClientRect();
                        if (!inMessageBand(r)) return;
                        const text = clean(el.innerText || el.textContent || '');
                        if (badText(text)) return;
                        if (text.length > 500 || text.split('\\n').length > 4) return;
                        const childSame = Array.from(el.children || []).some(ch => clean(ch.innerText || ch.textContent || '') === text);
                        if (childSame) return;
                        const cls = classTrail(el);
                        const sender = /item-myself|myself|self|mine|right/.test(cls)
                            || r.left > vw * 0.55
                            || (r.left > vw * 0.43 && r.right > vw * 0.82)
                            ? 'me' : 'hr';
                        candidates.push({sender, content:text, status: sender === 'me' ? pickStatus(el.parentElement ? el.parentElement.innerText : '') : '', top:r.top, left:r.left});
                    });
                    candidates.sort((a,b) => a.top - b.top || a.left - b.left);
                    for (const c of candidates) result.push(c);
                }
                const seen = new Set();
                return result
                    .sort((a,b) => a.top - b.top || a.left - b.left)
                    .filter(m => {
                        const key = `${m.sender}\\n${m.content}\\n${Math.round(m.top / 4)}`;
                        if (seen.has(key)) return false;
                        seen.add(key);
                        return true;
                    })
                    .map(({sender, content, status}) => ({sender, content, status}));
            }""")
            return raw or []
        except Exception:
            return []

    def _scroll_chat_messages(self, direction: str) -> bool:
        """滚动右侧聊天消息容器，direction=up/down/top。"""
        try:
            return bool(
                self.page.evaluate(
                    """(direction) => {
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const findContainer = () => {
                        const item = document.querySelector('li.message-item, li[class*="message-item"], [class*="message"] [class*="bubble"], [class*="chat"] [class*="text"]');
                        let el = item;
                        while (el && el !== document.body) {
                            const s = getComputedStyle(el);
                            if (visible(el) && /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 20) return el;
                            el = el.parentElement;
                        }
                        const candidates = [];
                        document.querySelectorAll('div, ul, section').forEach(node => {
                            if (!visible(node) || node.scrollHeight <= node.clientHeight + 20) return;
                            const r = node.getBoundingClientRect();
                            if (r.left < (window.innerWidth || 1200) * 0.32) return;
                            candidates.push({node, area: r.width * r.height, right: r.right});
                        });
                        candidates.sort((a, b) => b.area - a.area || b.right - a.right);
                        return candidates[0] && candidates[0].node;
                    };
                    const el = findContainer();
                    if (!el) return false;
                    const before = el.scrollTop;
                    if (direction === 'top') {
                        el.scrollTop = 0;
                    } else if (direction === 'up') {
                        el.scrollTop = Math.max(0, el.scrollTop - Math.max(300, el.clientHeight * 0.9));
                    } else {
                        el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + Math.max(300, el.clientHeight * 0.9));
                    }
                    return Math.abs(el.scrollTop - before) > 2;
                }""",
                    direction,
                )
            )
        except Exception:
            return False

    def read_all_messages(self, max_scrolls: int = 25) -> List[dict]:
        """尽量向上加载历史，再从顶部向下收集当前会话的所有可读消息。"""
        try:
            # 先不断向上滚动，促使 BOSS 加载更早的历史消息。
            stable = 0
            for _ in range(max_scrolls):
                moved = self._scroll_chat_messages("up")
                pause(0.15, 0.3)
                if not moved:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0

            self._scroll_chat_messages("top")
            pause(0.2, 0.4)

            seen = set()
            all_msgs = []
            stable = 0
            for _ in range(max_scrolls * 2):
                for msg in self.read_visible_messages():
                    content = (msg.get("content") or "").strip()
                    if not content:
                        continue
                    sender = msg.get("sender", "hr")
                    status = msg.get("status", "")
                    key = (sender, content, status)
                    if key in seen:
                        continue
                    seen.add(key)
                    all_msgs.append({"sender": sender, "content": content, "status": status})
                moved = self._scroll_chat_messages("down")
                pause(0.12, 0.25)
                if not moved:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0

            return all_msgs or self.read_visible_messages()
        except Exception:
            return self.read_visible_messages()

    def open_conversation_by_name(self, hr_name: str) -> bool:
        """在聊天页中按 HR 名字定位并打开对应会话。"""
        try:
            current_url = self.page.url
            if "/web/geek/chat" not in current_url:
                self.page.goto("https://www.zhipin.com/web/geek/chat", wait_until="load", timeout=45000)
                pause(2, 3)

            # 优先用 Playwright 文本选择器点击列表项。BOSS 的左栏布局会随宽度变化，
            # 不能强依赖元素在屏幕左半边。
            for sel in [
                f'li[role="listitem"]:has-text("{hr_name}")',
                f'.user-list li:has-text("{hr_name}")',
                f'[class*="friend"]:has-text("{hr_name}")',
                f'text="{hr_name}"',
            ]:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(force=True, timeout=3000)
                        pause(1, 2)
                        return True
                except Exception:
                    pass

            # 兜底：在 DOM 中找包含 HR 名的最小可点击会话容器并触发点击。
            clicked = self.page.evaluate(
                """(name) => {
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const candidates = [];
                    const selectors = [
                        '.user-list li', 'li[role="listitem"]', '.friend-content',
                        '[class*="friend"]', '[class*="conversation"]', '[class*="chat-item"]'
                    ];
                    document.querySelectorAll(selectors.join(',')).forEach(el => {
                        const text = (el.innerText || '');
                        if (text.length < 3 || text.length > 200) return;
                        if (!text.includes(name)) return;
                        if (!visible(el)) return;
                        const rect = el.getBoundingClientRect();
                        const nameEl = el.querySelector('.name-text, [class*="name"]');
                        const nameText = (nameEl && nameEl.innerText || '').trim();
                        const exact = nameText === name || text.split('\\n').some(line => line.trim() === name);
                        candidates.push({el: el, exact: exact ? 1 : 0, area: rect.width * rect.height, top: rect.top});
                    });
                    candidates.sort((a,b) => b.exact - a.exact || a.area - b.area || a.top - b.top);
                    for (const c of candidates) {
                        try {
                            c.el.scrollIntoView({block: 'center'});
                            const r = c.el.getBoundingClientRect();
                            const opts = {bubbles: true, cancelable: true, view: window, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
                            c.el.dispatchEvent(new MouseEvent('mousedown', opts));
                            c.el.dispatchEvent(new MouseEvent('mouseup', opts));
                            c.el.dispatchEvent(new MouseEvent('click', opts));
                            return true;
                        } catch(e) {}
                    }
                    return false;
                }""",
                hr_name,
            )
            if clicked:
                pause(1, 2)
                return True
            return False
        except Exception as e:
            print(f"  ⚠️ 打开会话失败 ({hr_name}): {e}")
            return False

    def accept_location_card(self) -> bool:
        """点击 BOSS 工作地点确认卡片里的「可以接受」。"""
        try:
            clicked = self.page.evaluate(
                """() => {
                    const visible = el => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const disabled = el => {
                        if (!el) return true;
                        const cls = String(el.className || '').toLowerCase();
                        const s = getComputedStyle(el);
                        return !!el.disabled ||
                            el.getAttribute('disabled') !== null ||
                            el.getAttribute('aria-disabled') === 'true' ||
                            cls.includes('disabled') ||
                            cls.includes('disable') ||
                            s.pointerEvents === 'none';
                    };
                    const clickLikeUser = el => {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        const r = el.getBoundingClientRect();
                        const opts = {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            clientX: r.left + r.width / 2,
                            clientY: r.top + r.height / 2,
                        };
                        el.dispatchEvent(new MouseEvent('mousedown', opts));
                        el.dispatchEvent(new MouseEvent('mouseup', opts));
                        el.dispatchEvent(new MouseEvent('click', opts));
                    };
                    const candidates = [];
                    document.querySelectorAll('button,a,span,div,[role="button"]').forEach(el => {
                        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '').trim();
                        if (text !== '可以接受' && !text.endsWith('可以接受')) return;
                        let target = el.closest('button,a,[role="button"],[class*="btn"],[class*="button"]') || el;
                        if (!visible(target) || disabled(target)) return;
                        let p = target;
                        let context = '';
                        for (let i = 0; i < 8 && p; i += 1, p = p.parentElement) {
                            context += ' ' + (p.innerText || p.textContent || '');
                        }
                        if (!context.includes('工作地点') && !context.includes('是否接受')) return;
                        const r = target.getBoundingClientRect();
                        candidates.push({el: target, top: r.top, area: r.width * r.height});
                    });
                    candidates.sort((a, b) => b.top - a.top || a.area - b.area);
                    for (const c of candidates) {
                        try {
                            clickLikeUser(c.el);
                            return true;
                        } catch(e) {}
                    }
                    return false;
                }"""
            )
            if clicked:
                print("  [地点卡片] 已点击「可以接受」")
                pause(1, 2)
                return True
            return False
        except Exception as e:
            print(f"  ⚠️ accept_location_card 失败: {e}")
            return False

    def accept_resume_request_card(self) -> bool:
        """点击 HR 附件简历请求卡片里的「同意」。"""
        try:
            clicked = self.page.evaluate(
                """() => {
                    const visible = el => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const disabled = el => {
                        if (!el) return true;
                        const cls = String(el.className || '').toLowerCase();
                        const s = getComputedStyle(el);
                        return !!el.disabled ||
                            el.getAttribute('disabled') !== null ||
                            el.getAttribute('aria-disabled') === 'true' ||
                            cls.includes('disabled') ||
                            cls.includes('disable') ||
                            s.pointerEvents === 'none';
                    };
                    const clickLikeUser = el => {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        const r = el.getBoundingClientRect();
                        const opts = {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                            clientX: r.left + r.width / 2,
                            clientY: r.top + r.height / 2,
                        };
                        el.dispatchEvent(new MouseEvent('mousedown', opts));
                        el.dispatchEvent(new MouseEvent('mouseup', opts));
                        el.dispatchEvent(new MouseEvent('click', opts));
                    };
                    const candidates = [];
                    document.querySelectorAll('button,a,span,div,[role="button"]').forEach(el => {
                        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '').trim();
                        if (text !== '同意' && !text.endsWith('同意')) return;
                        let target = el.closest('button,a,[role="button"],[class*="btn"],[class*="button"]') || el;
                        if (!visible(target) || disabled(target)) return;
                        let p = target;
                        let context = '';
                        for (let i = 0; i < 8 && p; i += 1, p = p.parentElement) {
                            context += ' ' + (p.innerText || p.textContent || '');
                        }
                        if (!context.includes('附件简历') && !context.includes('简历')) return;
                        const r = target.getBoundingClientRect();
                        candidates.push({el: target, top: r.top, area: r.width * r.height});
                    });
                    candidates.sort((a, b) => b.top - a.top || a.area - b.area);
                    for (const c of candidates) {
                        try {
                            clickLikeUser(c.el);
                            return true;
                        } catch(e) {}
                    }
                    return false;
                }"""
            )
            if clicked:
                print("  [简历卡片] 已点击「同意」")
                pause(1, 2)
                return True
            return False
        except Exception as e:
            print(f"  ⚠️ accept_resume_request_card 失败: {e}")
            return False

    def send_message(self, text: str, fast: bool = True) -> bool:
        """逐字模拟键盘输入 + Enter 发送，确保 BOSS 检测到输入事件。"""
        try:
            input_el = self._wait_for_chat_input(timeout_ms=8000)
            if not input_el:
                print("  ⚠️ send_message: 未找到聊天输入框")
                return False

            # 点击输入框激活
            try:
                input_el.click()
                time.sleep(0.15)
            except Exception:
                pass

            # 清除已有内容
            try:
                input_el.fill("")
            except Exception:
                try:
                    self.page.keyboard.press("Control+a")
                    time.sleep(0.05)
                    self.page.keyboard.press("Backspace")
                    time.sleep(0.05)
                except Exception:
                    pass

            # 优先 fill 触发 input 事件，失败再模拟键盘输入。
            try:
                input_el.fill(text)
            except Exception:
                try:
                    input_el.click()
                except Exception:
                    pass
                delay = 20 if fast else 40
                self.page.keyboard.type(text, delay=delay)

            typed = self._read_input_text(input_el)
            prefix = text[: min(6, len(text))]
            if prefix not in typed:
                try:
                    input_el.click()
                    self.page.keyboard.type(text, delay=20 if fast else 40)
                    typed = self._read_input_text(input_el)
                except Exception:
                    pass
            if prefix not in typed:
                print("  ⚠️ send_message: 输入框未写入招呼语")
                return False

            pause(0.3, 0.6)

            # 按 Enter 发送；如果 Enter 只换行或没有触发发送，再点发送按钮。
            self.page.keyboard.press("Enter")
            if self._wait_input_cleared(input_el, timeout_sec=3):
                return True

            send_btn = self._find_element(SELECTORS["chat_send_button"], timeout_ms=1500)
            if send_btn:
                try:
                    self._safe_click(send_btn)
                    if self._wait_input_cleared(input_el, timeout_sec=3):
                        return True
                except Exception:
                    pass

            try:
                self.page.keyboard.press("Enter")
                if self._wait_input_cleared(input_el, timeout_sec=2):
                    return True
            except Exception:
                pass

            print("  ⚠️ send_message: 输入框未清空，未确认发送成功")
            return False
        except Exception as e:
            print(f"  ⚠️ send_message 失败: {e}")
            return False

    def _get_chat_security_id(self, hr_name: str = "") -> str:
        """从 BOSS API 或页面提取对方 securityId。"""
        import re

        for attempt in range(3):  # 重试3次
            try:
                # 方式1: 页面 HTML 正则搜
                html = self.page.content()
                m = re.search(r'securityId["\']?\s*[:=]\s*["\']([A-Za-z0-9_~+/=-]{30,})["\']', html)
                if m:
                    return m.group(1)

                # 方式2: JS 全局对象
                sid = self.page.evaluate("""() => {
                    for (const key of Object.keys(window)) {
                        try {
                            const v = window[key];
                            if (!v || typeof v !== 'object') continue;
                            if (v.securityId) return v.securityId;
                        } catch(e) {}
                    }
                    return '';
                }""")
                if sid:
                    return sid

                # 方式3: BOSS API 获取会话列表, 按 HR 名匹配
                encrypt_id = ""
                try:
                    encrypt_id = self.page.evaluate("""() => {
                        for (const key of Object.keys(window)) {
                            try { if (window[key] && window[key].encryptSystemId) return window[key].encryptSystemId; } catch(e) {}
                        }
                        return '';
                    }""")
                except Exception:
                    pass

                if encrypt_id and hr_name:
                    url = f"https://www.zhipin.com/wapi/zprelation/friend/geekFilterByLabel?labelId=0&encryptSystemId={encrypt_id}"
                    data = self.page.evaluate(
                        """async (url) => {
                        const r = await fetch(url, {headers:{'Accept':'application/json','x-requested-with':'XMLHttpRequest'}, credentials:'include'});
                        return await r.json();
                    }""",
                        url,
                    )
                    friends = (data or {}).get("zpData", {}).get("friends", [])
                    for f in friends:
                        fn = (f.get("bossName") or f.get("realName") or "").strip()
                        if fn == hr_name:
                            return f.get("securityId", "")

                if attempt < 2:
                    print(f"  [securityId] 第{attempt + 1}次获取失败，重试...")
                    pause(1, 2)

            except Exception as e:
                print(f"  [securityId] 获取异常: {e}")
                if attempt < 2:
                    pause(1, 2)

        print(f"  ⚠️ securityId 获取失败（3次重试），HR: {hr_name}")
        return ""

    def send_wechat(self, hr_name: str = "") -> bool:
        """通过 BOSS API 发起交换，等弹窗出现后点「确定」。"""
        try:
            sid = self._get_chat_security_id(hr_name)

            if sid:
                self.page.evaluate(
                    """
                    async (sid) => {
                        await fetch('https://www.zhipin.com/wapi/zpchat/exchange/test', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded', 'x-requested-with': 'XMLHttpRequest'},
                            body: 'securityId=' + encodeURIComponent(sid) + '&type=2&friendSource=0',
                            credentials: 'include',
                        });
                    }
                """,
                    sid,
                )
                print("  [换微信] API /exchange/test 已调用")
            else:
                btn = self._find_element(SELECTORS["wechat_share_btn"], timeout_ms=5000)
                if not btn:
                    print("  ⚠️ send_wechat: 无法获取 securityId 且未找到按钮")
                    return False
                btn.click()
                print("  [换微信] 已点击换微信按钮")

            # 等弹窗 → 点「确定」
            confirm_clicked = self.page.evaluate("""() => {
                return new Promise((resolve) => {
                    let tries = 0;
                    const check = () => {
                        // 先找「确定与对方交换微信吗？」弹窗里的确定按钮
                        const btns = document.querySelectorAll('span');
                        for (const b of btns) {
                            if (b.innerText.trim() === '确定' && b.offsetParent !== null) {
                                const parent = b.closest('.secure-exchange, .sentence-popover, [class*="exchange"], [class*="popover"]');
                                if (parent) {
                                    b.click();
                                    resolve(true);
                                    return;
                                }
                            }
                        }
                        // 兜底：任何可见的"确定"按钮
                        const all = document.querySelectorAll('.btn-sure-v2, span');
                        for (const el of all) {
                            if (el.innerText.trim() === '确定' && el.offsetParent !== null && !el.closest('.btn-outline-v2')) {
                                el.click();
                                resolve(true);
                                return;
                            }
                        }
                        if (++tries < 30) setTimeout(check, 300);
                        else resolve(false);
                    };
                    check();
                });
            }""")
            if confirm_clicked:
                pause(0.5, 1)
                print("  [换微信] 已点确定按钮")
                return True

            print("  [换微信] 超时: 未找到确定按钮")
            return False

        except Exception as e:
            print(f"  ⚠️ send_wechat 失败: {e}")
            return False

    def send_phone(self, hr_name: str = "") -> bool:
        """通过 BOSS API 交换手机号（type=1），等弹窗出现后点「确定」。"""
        try:
            sid = self._get_chat_security_id(hr_name)

            if sid:
                self.page.evaluate(
                    """
                    async (sid) => {
                        await fetch('https://www.zhipin.com/wapi/zpchat/exchange/test', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded', 'x-requested-with': 'XMLHttpRequest'},
                            body: 'securityId=' + encodeURIComponent(sid) + '&type=1&friendSource=0',
                            credentials: 'include',
                        });
                    }
                """,
                    sid,
                )
                print("  [换电话] API /exchange/test (type=1) 已调用")
            else:
                btn = self._find_element(SELECTORS["phone_share_btn"], timeout_ms=5000)
                if not btn:
                    print("  ⚠️ send_phone: 无法获取 securityId 且未找到按钮")
                    return False
                btn.click()
                print("  [换电话] 已点击换电话按钮")

            # 等弹窗 → 点「确定」
            confirm_clicked = self.page.evaluate("""() => {
                return new Promise((resolve) => {
                    let tries = 0;
                    const check = () => {
                        const btns = document.querySelectorAll('span');
                        for (const b of btns) {
                            if (b.innerText.trim() === '确定' && b.offsetParent !== null) {
                                const parent = b.closest('.secure-exchange, .sentence-popover, .panel-contact, [class*="exchange"], [class*="popover"]');
                                if (parent) {
                                    b.click();
                                    resolve(true);
                                    return;
                                }
                            }
                        }
                        const all = document.querySelectorAll('.btn-sure-v2, span');
                        for (const el of all) {
                            if (el.innerText.trim() === '确定' && el.offsetParent !== null && !el.closest('.btn-outline-v2')) {
                                el.click();
                                resolve(true);
                                return;
                            }
                        }
                        if (++tries < 30) setTimeout(check, 300);
                        else resolve(false);
                    };
                    check();
                });
            }""")
            if confirm_clicked:
                pause(0.5, 1)
                print("  [换电话] 已点确定按钮")
                return True

            print("  [换电话] 超时: 未找到确定按钮")
            return False

        except Exception as e:
            print(f"  ⚠️ send_phone 失败: {e}")
            return False

    def send_resume(self) -> bool:
        """点击「发简历」按钮，选中已上传简历后点「发送」确认。"""
        try:
            btn = self._find_element(SELECTORS["resume_attach_btn"], timeout_ms=5000)
            if not btn:
                print("  ⚠️ send_resume: 未找到发简历按钮")
                return False
            btn.click()
            print("  [发简历] 已点击发简历按钮")
            pause(1, 2)

            selected = self.page.evaluate("""() => {
                return new Promise((resolve) => {
                    let tries = 0;
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const clickLikeUser = el => {
                        const r = el.getBoundingClientRect();
                        const opts = {bubbles:true, cancelable:true, view:window, clientX:r.left+r.width/2, clientY:r.top+r.height/2};
                        el.dispatchEvent(new MouseEvent('mousedown', opts));
                        el.dispatchEvent(new MouseEvent('mouseup', opts));
                        el.dispatchEvent(new MouseEvent('click', opts));
                    };
                    const check = () => {
                        const dialogs = Array.from(document.querySelectorAll('.boss-popup__content, .dialog-container, .choose-resume-dialog, [class*="resume"]'))
                            .filter(visible);
                        const root = dialogs.find(d => (d.innerText || '').includes('请选择需要投递的简历')) || dialogs.find(d => (d.innerText || '').includes('简历')) || document.body;
                        const candidates = [];
                        root.querySelectorAll('div, li, label, section').forEach(el => {
                            if (!visible(el)) return;
                            const text = (el.innerText || '').trim();
                            if (!/简历|\\.pdf|\\.doc|\\.docx/i.test(text)) return;
                            if (/管理附件|预览|发送|取消|请选择/.test(text)) return;
                            const r = el.getBoundingClientRect();
                            if (r.width < 120 || r.height < 28) return;
                            candidates.push({el, area:r.width*r.height, top:r.top, left:r.left});
                        });
                        candidates.sort((a,b) => a.area - b.area || a.top - b.top || a.left - b.left);
                        if (candidates.length) {
                            clickLikeUser(candidates[0].el);
                            resolve(true);
                            return;
                        }
                        if (++tries > 25) resolve(false);
                        else setTimeout(check, 200);
                    };
                    check();
                });
            }""")
            if selected:
                print("  [发简历] 已选中简历附件")
                pause(0.5, 1)
            else:
                print("  ⚠️ send_resume: 未能选中简历附件")

            confirm_clicked = self.page.evaluate("""() => {
                return new Promise((resolve) => {
                    let tries = 0;
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const disabled = el => {
                        const cls = el.className || '';
                        return el.disabled || el.getAttribute('disabled') !== null || /disabled|disable/.test(cls) || getComputedStyle(el).pointerEvents === 'none';
                    };
                    const clickLikeUser = el => {
                        const r = el.getBoundingClientRect();
                        const opts = {bubbles:true, cancelable:true, view:window, clientX:r.left+r.width/2, clientY:r.top+r.height/2};
                        el.dispatchEvent(new MouseEvent('mousedown', opts));
                        el.dispatchEvent(new MouseEvent('mouseup', opts));
                        el.dispatchEvent(new MouseEvent('click', opts));
                    };
                    const check = () => {
                        const btns = Array.from(document.querySelectorAll('button, span, a, div'))
                            .filter(visible)
                            .filter(el => (el.innerText || el.textContent || '').trim() === '发送')
                            .filter(el => !disabled(el));
                        btns.sort((a,b) => {
                            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                            return rb.left - ra.left || rb.top - ra.top;
                        });
                        if (btns.length) {
                            clickLikeUser(btns[0]);
                            resolve(true);
                            return;
                        }
                        if (++tries > 35) resolve(false);
                        else setTimeout(check, 200);
                    };
                    check();
                });
            }""")
            if confirm_clicked:
                pause(0.5, 1)
                print("  [发简历] 已点发送按钮")
                return True

            print("  [发简历] 超时: 发送按钮未变为可点击")
            return False
        except Exception as e:
            print(f"  ⚠️ send_resume 失败: {e}")
            return False

    # ══════════════════════════════════════
    #  页面扫描 & 一键投递
    # ══════════════════════════════════════

    def scan_current_page(self) -> List[dict]:
        """扫描当前BOSS搜索结果页，提取所有可见岗位卡片。不跳转，只读当前页。"""
        print(f"  [扫描] 开始扫描当前页面...")
        self._scroll_all()
        jobs = self._extract_job_cards()
        if not jobs:
            lines = [l.strip() for l in self.page.inner_text("body").split("\n") if l.strip()]
            sal_idx = [i for i, l in enumerate(lines) if re.search(r"\d+[-~]\d+K", decode_salary(l), re.I)]
            for n, si in enumerate(sal_idx):
                if n > 0 and si - sal_idx[n - 1] < 3:
                    continue
                if si == 0:
                    continue
                title = lines[si - 1]
                if not (2 < len(title) < 60):
                    continue
                salary = decode_salary(lines[si])
                company = exp = edu = city = ""
                end = sal_idx[n + 1] if n + 1 < len(sal_idx) else min(si + 10, len(lines))
                for j in range(si + 1, min(end, len(lines))):
                    ln = lines[j]
                    if "经验" in ln or "应届" in ln:
                        exp = ln
                    elif re.search(r"本科|硕士|博士|大专|学历不限", ln):
                        edu = ln
                    elif "·" in ln and len(ln) < 30:
                        city = ln
                    elif (
                        not company
                        and len(ln) > 2
                        and len(ln) < 40
                        and not re.search(r"年|学历|大专|本科|硕士|博士|不限|应届|·", ln)
                    ):
                        company = ln
                jobs.append(
                    {
                        "title": title,
                        "salary": salary,
                        "company": company,
                        "experience": exp,
                        "education": edu,
                        "city": city,
                        "url": "",
                        "description": "",
                        "hr_name": "",
                        "hr_title": "",
                    }
                )
            links = self._extract_links()
            if links:
                lm = {l["title"][:12]: l["href"] for l in links if l["title"][:12]}
                for j in jobs:
                    if not j["url"] and j["title"][:12] in lm:
                        j["url"] = lm[j["title"][:12]]
        print(f"  [扫描] 从当前页面提取到 {len(jobs)} 个岗位")
        return jobs

    def scan_and_apply_current_page(self, greeting_template: Optional[str] = None) -> dict:
        """扫描当前页面全部岗位 → 一键批量投递。"""
        jobs = self.scan_current_page()
        if not jobs:
            return {"success": False, "message": "当前页面未找到任何岗位", "scanned": 0, "applied": 0}
        urls = [j["url"] for j in jobs if j.get("url")]
        if not urls:
            return {"success": False, "message": "扫描到的岗位没有有效URL", "scanned": len(jobs), "applied": 0}
        results = self.apply_batch(urls, greeting_template)
        success_count = sum(1 for r in results if r.get("success"))
        return {
            "success": success_count > 0,
            "message": f"扫描 {len(jobs)} 个岗位，投递 {success_count}/{len(urls)}",
            "scanned": len(jobs),
            "applied": success_count,
            "results": results,
        }

    # ══════════════════════════════════════
    #  监控周期（供后台循环调用）
    # ══════════════════════════════════════

    def run_chat_monitor_cycle(self) -> dict:
        """
        一个完整的监控周期:
        1. 导航到聊天页
        2. 扫描未读会话
        3. 对每个未读会话: 打开→读消息→存库→AI回复
        """
        result = {
            "checked": 0,
            "new_messages": 0,
            "replies_sent": 0,
            "location_cards_accepted": 0,
            "resume_cards_accepted": 0,
        }

        # 只在不在聊天页时才导航（避免每轮刷新页面，触发 BOSS 登录检查）
        current_url = self.page.url
        need_nav = "/web/geek/chat" not in current_url
        if need_nav:
            if not self.navigate_to_chat():
                print("  [监控] 导航到聊天页失败")
                return result
        else:
            # 已在聊天页，轻量点击「未读」Tab 即可
            for sel in ['span.label-name:has-text("未读")', '.label-name:has-text("未读")']:
                try:
                    tab = self.page.locator(sel).first
                    if tab.is_visible():
                        tab.click()
                        pause(0.5, 1)
                        break
                except Exception:
                    pass

        if not self.check_page_safety():
            print("  [监控] 安全检查未通过（登录过期/验证码等）")
            return result

        conversations = self.poll_conversation_list()
        source = "unread"
        if not conversations:
            self._click_chat_label("全部")
            pause(0.5, 1)
            self._reset_conversation_list_scroll()
            recent_limit = int(get_setting("monitor_recent_scan_limit", "12"))
            recent_limit = max(1, min(recent_limit, 50))
            conversations = self.poll_conversation_list()[:recent_limit]
            source = "recent"
        result["checked"] = len(conversations)
        print(f"  [监控] 扫描到 {len(conversations)} 个会话 ({source})")
        # 始终打印 body 内容用于调试
        try:
            preview = (self.page.inner_text("body") or "")[:800].replace("\n", " | ")
            print(f"  [监控] Body: {preview}")
        except Exception:
            pass

        from boss_state import list_active_conversations

        known_convs = list_active_conversations()
        print(f"  [监控] 数据库已知活跃会话: {len(known_convs)}")

        # 已在导航时切到「未读」Tab；未读为空时会兜底扫描全部里的最近会话。
        if not conversations:
            print(f"  [监控] 无可扫描会话，跳过本轮")
            return result
        batch_limit = int(get_setting("monitor_unread_batch_limit", "10"))
        batch_limit = max(1, min(batch_limit, 30))
        if len(conversations) > batch_limit:
            print(f"  [监控] 会话: {len(conversations)} 个，本轮处理前{batch_limit}个")
            conversations = conversations[:batch_limit]

        for conv_data in conversations:
            text = conv_data.get("text", "")
            has_unread = conv_data.get("has_unread", False)
            element = conv_data.get("element")

            if not text:
                continue

            # 尝试匹配已知会话：用提取的 HR 名字精确匹配
            matched_conv = None
            extracted_name = conv_data.get("hr_name", "")
            for kc in known_convs:
                kc_name = kc.get("hr_name", "")
                if kc_name and extracted_name and kc_name == extracted_name:
                    matched_conv = kc
                    break

            if not matched_conv:
                for kc in known_convs:
                    kc_name = kc.get("hr_name", "")
                    if kc_name and len(kc_name) >= 3 and kc_name in text:
                        matched_conv = kc
                        break

            if not matched_conv:
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                hr_name = conv_data.get("hr_name", "") or lines[0] if lines else ""
                hr_name = hr_name[:20] if len(hr_name) > 20 else hr_name

                # 过滤无效名称
                skip_keywords = [
                    "消息",
                    "联系人",
                    "沟通",
                    "设置",
                    "搜索",
                    "我的",
                    "首页",
                    "已沟通",
                    "继续沟通",
                    "新对话",
                    "系统",
                    "通知",
                    "BOSS",
                    "在线",
                    "离线",
                    "刚刚",
                    "分钟",
                    "小时",
                    "昨天",
                    "简历",
                    "附件",
                    "上传",
                    "制作",
                    "更新",
                    "AI",
                ]
                is_valid = (
                    hr_name
                    and len(hr_name) >= 2
                    and not hr_name.isdigit()
                    and not any(kw == hr_name for kw in skip_keywords)
                    and not any(kw in hr_name and len(hr_name) <= len(kw) + 1 for kw in skip_keywords)
                )
                if not is_valid:
                    print(f"  [监控] 跳过无效会话名: '{hr_name}' (原文: {text[:50]})")
                    continue

                conv_id = get_or_create_conversation(
                    None, hr_name, conv_data.get("company", ""), conv_data.get("job_title", "")
                )
                known_convs = list_active_conversations()
                matched_conv = get_conversation(conv_id)
                if not matched_conv:
                    continue
                print(f"  [监控] 新建会话: {hr_name}")
                # 标记用于 WebSocket 广播
                result.setdefault("new_conversations", []).append(hr_name)
            else:
                conv_id = matched_conv["id"]
                # 提取的名字比 DB 更精确时自动修正
                if extracted_name and len(extracted_name) >= 2:
                    old_name = matched_conv.get("hr_name", "")
                    if old_name != extracted_name and (
                        old_name in extracted_name or extracted_name in old_name or len(extracted_name) < len(old_name)
                    ):
                        try:
                            from boss_state import get_db as _gdb2

                            _gdb2().execute("UPDATE conversations SET hr_name=? WHERE id=?", (extracted_name, conv_id))
                            _gdb2().commit()
                            matched_conv["hr_name"] = extracted_name
                        except Exception:
                            pass

            # 从会话文本里提取公司名（格式：HR名+公司名+岗位）
            if not matched_conv.get("hr_company"):
                company_info = text.split("\n")[0] if "\n" in text else text
                import re as _re3

                hr_name_part = matched_conv.get("hr_name", "")
                if hr_name_part and len(hr_name_part) >= 2:
                    company_info = company_info.replace(hr_name_part, "", 1)
                # 去掉时间/状态/括号等
                company_info = _re3.sub(r"\d{1,2}:\d{2}|\[.*?\]|送达|已读|未读", "", company_info)
                # 提取公司名（纯中文 4-12字）
                m = _re3.search(r"[\u4e00-\u9fa5]{4,12}", company_info)
                if m:
                    company = m.group()
                    try:
                        from boss_state import get_db as _gdb3

                        _gdb3().execute("UPDATE conversations SET hr_company=? WHERE id=?", (company, conv_id))
                        _gdb3().commit()
                        matched_conv["hr_company"] = company
                        print(f"  [监控] 提取公司名: {company}")
                    except Exception:
                        pass

            if matched_conv.get("status") != "active":
                continue
            if not matched_conv.get("auto_reply_enabled"):
                continue

            # 读取消息：打开会话从 DOM 提取
            hr_name_to_open = matched_conv["hr_name"]
            opened = self.open_conversation_by_name(hr_name_to_open)
            if not opened and len(hr_name_to_open) > 4:
                short = re.match(r"^[\u4e00-\u9fff]{2,3}", hr_name_to_open)
                if short:
                    opened = self.open_conversation_by_name(short.group(0))
            if not opened:
                print(f"  [监控] 无法打开会话: {hr_name_to_open}")
                continue
            pause(1, 2)
            msgs = self.read_all_messages(max_scrolls=12)
            print(f"  [监控] 会话 {matched_conv.get('hr_name')}: 读到 {len(msgs)} 条消息")
            resume_accepted = self.accept_resume_request_card()
            if resume_accepted:
                result["resume_cards_accepted"] += 1
                mark_resume_sent(conv_id)
                matched_conv["resume_sent"] = 1
                monitor_log(f"[监控] {matched_conv.get('hr_name')} 已点击附件简历请求「同意」")
            location_accepted = self.accept_location_card()
            if location_accepted:
                result["location_cards_accepted"] += 1
                monitor_log(f"[监控] {matched_conv.get('hr_name')} 已点击工作地点卡片「可以接受」")

            new_count = 0
            clean_msgs = []
            for msg in msgs:
                sender = msg.get("sender", "hr")
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                clean_msgs.append({"sender": sender, "content": content, "status": msg.get("status", "")})

            if clean_msgs:
                replace_conversation_messages(conv_id, clean_msgs)
                last_msg = clean_msgs[-1]
                update_conversation_last_message(conv_id, last_msg["content"], last_msg["sender"], 0)

                # 从 HR 消息里提取微信号
                if not matched_conv.get("hr_wechat"):
                    import re as _re

                    for m in clean_msgs:
                        if m["sender"] == "hr":
                            patterns = [
                                # wxid_xxxxxxxx 格式
                                r"(?:wxid|WXID)[_\-]?\s*[:：]?\s*([a-zA-Z0-9_-]{6,30})",
                                # 微信/VX/WeChat：xxx 格式
                                r"(?:微信|VX|vx|wechat|WeChat)[号：:]*\s*[:：]?\s*([a-zA-Z0-9_-]{4,30})",
                                # 加我/加V -> xxx
                                r"(?:加我|加V|找V|加个V)\s*[:：]?\s*([a-zA-Z0-9_-]{4,30})",
                                # 微信号 xxx（纯中文前缀）
                                r"\u5fae\u4fe1\u53f7\s+([a-zA-Z0-9_-]{4,30})",
                            ]
                            for pat in patterns:
                                match = _re.search(pat, m["content"])
                                if match:
                                    wx_id = match.group(1).strip()
                                    if wx_id and len(wx_id) >= 5:
                                        update_conversation_wechat(conv_id, wx_id)
                                        matched_conv["hr_wechat"] = wx_id
                                        result["wechat_exchanged"] = True
                                        print(f"  [监控] 提取HR微信: {wx_id}")
                                        break

            # 检测需要回复的 HR 消息：仅跳过纯 BOSS 系统通知（<80字且以系统模式开头）
            def _is_system_notification(content):
                content = content.strip()
                if len(content) > 80:
                    return False
                patterns = (
                    "你与该职位竞争者PK情况",
                    "竞争力分析",
                    "BOSS安全提示",
                    "系统消息",
                    "沟通分析",
                    "今日推荐",
                    "该Boss已查看了你的简历",
                    "对方已同意",
                    "对方已查看了您的附件简历",
                    "附件简历请求已发送",
                )
                return any(content.startswith(p) for p in patterns)

            unreplied_hr_msg = None
            hr_batch_signature = ""
            meaningful_msgs = [m for m in clean_msgs if not _is_system_notification(m["content"])]
            if meaningful_msgs:
                latest_msg = meaningful_msgs[-1]
                tail = " | ".join(f"{m['sender']}:{m['content'][:30]}" for m in meaningful_msgs[-4:])
                if latest_msg["sender"] == "hr":
                    import hashlib as _hashlib

                    last_me_idx = -1
                    for idx, msg in enumerate(meaningful_msgs):
                        if msg["sender"] == "me":
                            last_me_idx = idx
                    hr_batch = [
                        m["content"].strip()
                        for m in meaningful_msgs[last_me_idx + 1 :]
                        if m["sender"] == "hr" and m["content"].strip()
                    ]
                    if hr_batch:
                        unreplied_hr_msg = "\n".join(hr_batch)
                        hr_batch_signature = _hashlib.sha256("\n---\n".join(hr_batch).encode("utf-8")).hexdigest()
                        new_count = len(hr_batch)
                        monitor_log(
                            f"[监控] {matched_conv.get('hr_name')} 待回复HR批次({len(hr_batch)}条): "
                            f"{unreplied_hr_msg[:160]} | tail={tail}"
                        )
                else:
                    monitor_log(f"[监控] {matched_conv.get('hr_name')} 最新消息来自自己，跳过回复 | tail={tail}")
            else:
                monitor_log(f"[监控] {matched_conv.get('hr_name')} 未读到有效聊天消息，跳过回复")

            if unreplied_hr_msg:
                result["new_messages"] += 1

            # 自动回复
            auto_reply_enabled = get_setting("auto_reply_enabled", "false") == "true"
            if unreplied_hr_msg and auto_reply_enabled:
                today_replies = get_today_auto_reply_count()
                if today_replies >= MAX_AUTO_REPLY_PER_DAY:
                    continue

                try:
                    from boss_replier import (
                        decide_reply_strategy,
                        generate_reply,
                        is_phone_request,
                        is_resume_request,
                        is_wechat_request,
                    )

                    job_title = matched_conv.get("job_title", "")
                    job_company = matched_conv.get("hr_company", "")
                    job_desc = ""
                    app_id = matched_conv.get("application_id")
                    if app_id:
                        from boss_state import get_application

                        app = get_application(app_id)
                        if app:
                            job_desc = app.get("description") or ""
                            job_title = job_title or app.get("job_title", "")
                            job_company = job_company or app.get("company", "")

                    job_info = {
                        "title": job_title,
                        "company": job_company,
                        "description": job_desc,
                    }
                    style = get_setting("ai_reply_style", "professional")
                    resume = get_setting("resume_summary", "")
                    wechat = get_setting("wechat_id", "")

                    latest_conv_state = get_conversation(conv_id) or matched_conv
                    if hr_batch_signature and latest_conv_state.get("last_auto_reply_signature") == hr_batch_signature:
                        monitor_log(
                            f"[监控] {matched_conv.get('hr_name')} HR批次已回复过，跳过重复自动回复: {hr_batch_signature[:10]}"
                        )
                        continue

                    decision = decide_reply_strategy(conv_id, unreplied_hr_msg, job_info, resume, wechat)
                    decision_reason = decision.get("reason") or "无"
                    decision_tone = decision.get("tone") or style
                    decision_action = decision.get("action") or "none"
                    decision_interest = decision.get("interest") or ""
                    if decision_interest:
                        update_conversation_interest(conv_id, decision_interest)

                    monitor_log(
                        f"[监控] {matched_conv.get('hr_name')} AI决策: "
                        f"reply={decision.get('should_reply')} tone={decision_tone} "
                        f"action={decision_action} interest={decision_interest or '-'} reason={decision_reason}"
                    )

                    if decision_action == "accept_location" and not location_accepted:
                        location_accepted = self.accept_location_card()
                        if location_accepted:
                            result["location_cards_accepted"] += 1
                            monitor_log(f"[监控] {matched_conv.get('hr_name')} 已按AI决策点击工作地点卡片「可以接受」")

                    if not decision.get("should_reply"):
                        if hr_batch_signature:
                            mark_auto_reply_signature(conv_id, hr_batch_signature)
                        continue

                    reply, interest = generate_reply(
                        conv_id, unreplied_hr_msg, job_info, decision_tone or style, resume, wechat
                    )
                    reply = (reply or "").strip()
                    if reply:
                        latest_conv_state = get_conversation(conv_id) or matched_conv
                        last_auto_reply_text = (latest_conv_state.get("last_auto_reply_text") or "").strip()
                        if last_auto_reply_text == reply or message_exists(conv_id, reply, "me"):
                            if hr_batch_signature:
                                mark_auto_reply_signature(conv_id, hr_batch_signature, reply)
                            monitor_log(
                                f"[监控] {matched_conv.get('hr_name')} 相同回复已存在，跳过重复发送: {reply[:80]}"
                            )
                            continue

                        # 先执行发送操作（简历/微信/电话），确保AI说"已发送"时东西已经发出去了
                        msg_lower = unreplied_hr_msg.lower()

                        # 发简历：只在HR明确索要简历时触发；拒绝/不合适类消息优先排除。
                        if decision_action == "send_resume" or is_resume_request(unreplied_hr_msg):
                            if not matched_conv.get("resume_sent"):
                                print(f"  [监控] HR要简历，正在发送...")
                                if self.send_resume():
                                    from boss_state import mark_resume_sent

                                    mark_resume_sent(conv_id)
                                    pause(1, 2)

                        # 换微信：HR主动要联系方式时（排除"保持联系"等模糊表达）
                        if decision_action == "send_wechat" or is_wechat_request(unreplied_hr_msg):
                            if not matched_conv.get("hr_wechat"):
                                print(f"  [监控] HR要微信，正在发送...")
                                self.send_wechat(hr_name_to_open)
                                pause(1, 2)

                        # 换电话：HR明确要电话时，且未发送过
                        if decision_action == "send_phone" or is_phone_request(unreplied_hr_msg):
                            if not matched_conv.get("phone_shared"):
                                print(f"  [监控] HR要电话，正在发送...")
                                if self.send_phone(hr_name_to_open):
                                    from boss_state import mark_phone_shared

                                    mark_phone_shared(conv_id)
                                    pause(1, 2)

                        # 然后再发送AI回复
                        monitor_log(f"[监控] {matched_conv.get('hr_name')} AI回复准备发送: {reply[:80]}")
                        if self.send_message(reply):
                            add_message(conv_id, "me", reply, ai_generated=True)
                            update_conversation_last_message(conv_id, reply, "me", 0)
                            increment_daily_stat("auto_replies_sent")
                            result["replies_sent"] += 1
                            if interest:
                                update_conversation_interest(conv_id, interest)
                                print(f"  [监控] HR兴趣度: {interest}")
                            if hr_batch_signature:
                                mark_auto_reply_signature(conv_id, hr_batch_signature, reply)
                            monitor_log(f"[监控] {matched_conv.get('hr_name')} 回复已发送")
                        else:
                            monitor_log(f"[监控] {matched_conv.get('hr_name')} 回复发送失败")
                        pause(5, 15)
                except Exception as e:
                    monitor_log(f"[监控] {matched_conv.get('hr_name')} AI回复生成/发送异常: {e}")
            elif unreplied_hr_msg and not auto_reply_enabled:
                monitor_log(f"[监控] {matched_conv.get('hr_name')} 全局自动回复已关闭，跳过")

            # 下一个会话前确保输入框已清空，避免残留文字
            try:
                input_el = self.page.locator("#chat-input").first
                text = input_el.inner_text().strip()
                if text:
                    print(f"  [监控] 输入框残留文字「{text[:30]}...」，正在清空")
                    input_el.click()
                    self.page.keyboard.press("Control+a")
                    self.page.keyboard.press("Backspace")
                    pause(0.3, 0.5)
            except Exception:
                pass
            # 恢复本轮来源列表：未读轮询回未读；最近会话兜底回全部。
            self._click_chat_label("未读" if source == "unread" else "全部")
            pause(0.5, 1)

        print(f"  [监控] 本轮完成: 消息 {result['new_messages']}, 回复 {result['replies_sent']}")
        return result
