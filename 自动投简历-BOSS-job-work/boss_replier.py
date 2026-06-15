#!/usr/bin/env python3
"""
AI 回复生成 —— 调用 DeepSeek API 为 BOSS直聘聊天生成自动回复。
每次回复同时由 DeepSeek 根据对话上下文评估 HR 兴趣度 (high/medium/low)。
"""

import json
import re
import sys
from pathlib import Path

# 复用 interview/llm_client.py
sys.path.insert(0, str(Path(__file__).parent / "interview"))
from llm_client import llm_chat_deepseek

from boss_state import get_recent_messages, get_setting

DEFAULT_USER_REPLY_STYLE = """参考求职者本人平时的聊天语气：
- 中文表达简短直接，先解决问题，不绕弯子
- 礼貌但不过度客套，不要油腻热情，不要长篇解释
- 不自称AI、助手、系统、工具，也不要提自动化
- 不用表情符号，不说套话，不反复寒暄
- 能一句说清就一句，最多两句；只有HR问技术细节时才展开
- 对“好的、嗯嗯、收到、我看一下”这类确认消息不要继续回复
- 语气像本人在正常找工作沟通：自然、实在、克制"""

REJECTION_KEYWORDS = (
    "不太合适",
    "不合适",
    "不匹配",
    "不符合",
    "暂不考虑",
    "不考虑",
    "不通过",
    "未通过",
    "不满足",
    "不符合要求",
    "没有匹配",
    "另寻",
    "早日找到满意",
)

RESUME_REQUEST_KEYWORDS = (
    "发简历",
    "发送简历",
    "投简历",
    "传简历",
    "简历发",
    "简历给",
    "简历传",
    "简历投",
    "看看简历",
    "看下简历",
    "看一下简历",
    "要简历",
    "来份简历",
    "cv",
    "resume",
    "作品集",
)

WECHAT_REQUEST_KEYWORDS = (
    "加微信",
    "加个微信",
    "微信聊",
    "微信号",
    "换微信",
    "加v",
    "加个v",
    "v我",
    "vx",
    "wechat",
)

PHONE_REQUEST_KEYWORDS = (
    "电话",
    "手机号",
    "手机号码",
    "联系方式",
    "留个电话",
    "留电话",
)

LOCATION_ACCEPT_KEYWORDS = (
    "是否接受此工作地点",
    "接受此工作地点",
    "工作地点",
    "可以接受",
    "暂不考虑",
)

INTERVIEW_KEYWORDS = (
    "面试",
    "约面",
    "来面",
    "复试",
    "初试",
    "视频面",
    "线下面",
    "现场面",
    "到公司",
)

AVAILABILITY_KEYWORDS = (
    "到岗",
    "入职",
    "多久可以到",
    "多久能到",
    "什么时候能到",
    "最快什么时候",
    "最快多久",
)

LEAVE_REASON_KEYWORDS = (
    "离职原因",
    "为什么离职",
    "为啥离职",
    "怎么离职",
    "上一家公司",
    "上家公司",
    "离开上家",
)

OUTSOURCING_KEYWORDS = (
    "外包",
    "驻场",
    "外派",
    "项目外包",
    "到美的",
)

SALARY_EXPECTATION_KEYWORDS = (
    "期望薪资",
    "薪资期望",
    "期望多少",
    "薪资多少",
    "薪资要求",
    "期望工资",
    "工资期望",
)

SOCIAL_SECURITY_KEYWORDS = (
    "社保",
    "累计",
    "累积",
    "几个月",
    "多少月",
)

EDUCATION_VERIFY_KEYWORDS = (
    "全日制",
    "统招",
    "本科",
    "学历",
    "毕业证",
    "学位证",
    "学信网",
    "可查",
    "查得到",
)


NO_REPLY_ACK_WORDS = (
    "好",
    "好的",
    "好滴",
    "好哒",
    "嗯",
    "嗯嗯",
    "嗯呐",
    "哦",
    "哦哦",
    "收到",
    "了解",
    "明白",
    "可以",
    "行",
    "行的",
    "ok",
    "okay",
    "先这样",
    "辛苦了",
    "谢谢",
    "多谢",
)

REPLY_REQUIRED_HINTS = (
    "简历",
    "微信",
    "电话",
    "手机号",
    "面试",
    "方便",
    "什么时候",
    "几点",
    "薪资",
    "期望",
    "到岗",
    "项目",
    "经验",
    "介绍",
    "发",
    "加",
    "?",
    "？",
)


def is_rejection_message(text: str) -> bool:
    t = (text or "").strip().lower()
    if "是否接受此工作地点" in t or ("工作地点" in t and "可以接受" in t):
        return False
    return any(kw in t for kw in REJECTION_KEYWORDS)


def is_resume_request(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    if "简历" in t:
        return True
    return any(kw in t for kw in RESUME_REQUEST_KEYWORDS)


def is_wechat_request(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in WECHAT_REQUEST_KEYWORDS)


def is_phone_request(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in PHONE_REQUEST_KEYWORDS)


def is_location_accept_request(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return "是否接受此工作地点" in t or ("工作地点" in t and "可以接受" in t)


def is_interview_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in INTERVIEW_KEYWORDS)


def is_availability_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in AVAILABILITY_KEYWORDS)


def is_leave_reason_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in LEAVE_REASON_KEYWORDS)


def is_outsourcing_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in OUTSOURCING_KEYWORDS)


def is_salary_expectation_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return any(kw in t for kw in SALARY_EXPECTATION_KEYWORDS) or ("期望" in t and "薪" in t)


def is_social_security_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    return "社保" in t and any(kw in t for kw in ("累计", "累积", "几个月", "多少月", "多久"))


def is_education_verify_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if is_rejection_message(t):
        return False
    if "学信网" in t or "全日制" in t or "统招" in t:
        return True
    education_hits = sum(1 for kw in EDUCATION_VERIFY_KEYWORDS if kw in t)
    return education_hits >= 2


def common_question_reply(text: str) -> tuple[str, str]:
    """Return a fixed, user-approved answer for common interview questions."""
    parts = []
    if is_outsourcing_question(text):
        if "美的" in (text or ""):
            parts.append("外包到美的可以接受")
        else:
            parts.append("外包/驻场可以接受")
    if is_salary_expectation_question(text):
        parts.append("期望薪资8k左右，具体可以结合岗位再沟通")
    if is_social_security_question(text):
        parts.append("社保正常缴纳，具体累计月数我确认后可以补充")
    if is_education_verify_question(text):
        parts.append("是全日制本科毕业，学信网可查")
    if is_leave_reason_question(text):
        parts.append("离职主要是公司业务转型，原部门撤销了")
    if is_availability_question(text):
        parts.append("到岗这边周内可以")
    if is_interview_question(text):
        parts.append("面试优先线上更方便，时间您这边给个范围我尽量配合")

    if not parts:
        return "", ""
    if len(parts) == 1:
        if is_outsourcing_question(text):
            return ("外包到美的可以接受。" if "美的" in (text or "") else "外包/驻场可以接受。"), "high"
        if is_salary_expectation_question(text):
            return "期望薪资8k左右，具体可以结合岗位再沟通。", "high"
        if is_social_security_question(text):
            return "社保正常缴纳，具体累计月数我确认后可以补充。", "medium"
        if is_education_verify_question(text):
            return "是全日制本科毕业，学信网可查。", "high"
        if is_leave_reason_question(text):
            return "主要是公司业务转型，原部门撤销了，所以在看新的机会。", "high"
        if is_availability_question(text):
            return "周内可以到岗，具体时间可以配合流程再确认。", "high"
        return "可以沟通，优先线上面试更方便；时间您这边给个范围，我尽量配合。", "high"
    return "；".join(parts) + "。", "high"


def should_skip_auto_reply(text: str) -> bool:
    """Return True for HR acknowledgement/end messages that do not need another reply."""
    raw = (text or "").strip()
    if not raw:
        return True
    lower = raw.lower()
    if is_resume_request(lower) or is_rejection_message(lower):
        return False
    if any(hint in lower for hint in REPLY_REQUIRED_HINTS):
        return False

    compact = re.sub(r"[\s，。！？!?,.、~～…]+", "", lower)
    if compact in NO_REPLY_ACK_WORDS:
        return True
    if len(compact) <= 8 and any(compact.startswith(word) for word in NO_REPLY_ACK_WORDS):
        return True
    return False


def _decision(
    should_reply: bool,
    tone: str = "brief",
    action: str = "none",
    interest: str = "low",
    reason: str = "",
) -> dict:
    return {
        "should_reply": bool(should_reply),
        "tone": tone if tone in ("brief", "professional", "friendly", "enthusiastic", "cautious") else "brief",
        "action": action
        if action in ("none", "send_resume", "send_wechat", "send_phone", "accept_location")
        else "none",
        "interest": interest if interest in ("high", "medium", "low") else "low",
        "reason": reason,
    }


def _rule_based_decision(hr_message: str) -> dict | None:
    text = (hr_message or "").strip()
    lower = text.lower()
    compact = re.sub(r"[\s，。！？!?,.、~～…]+", "", lower)
    if not compact:
        return _decision(False, reason="空消息")

    if should_skip_auto_reply(text):
        return _decision(False, tone="brief", interest="medium", reason="HR只是确认或结束语")

    if is_rejection_message(text):
        return _decision(False, tone="brief", interest="low", reason="HR明确拒绝或认为不合适")

    if is_location_accept_request(text):
        return _decision(False, tone="brief", action="accept_location", interest="medium", reason="BOSS工作地点卡片，点击可以接受即可")

    if is_resume_request(text):
        return _decision(True, tone="brief", action="send_resume", interest="medium", reason="HR索要简历")

    if is_wechat_request(text):
        return _decision(True, tone="brief", action="send_wechat", interest="high", reason="HR索要联系方式")

    if is_phone_request(text):
        return _decision(True, tone="brief", action="send_phone", interest="high", reason="HR索要电话")

    fixed_reply, _ = common_question_reply(text)
    if fixed_reply:
        return _decision(True, tone="brief", interest="high", reason="HR询问常见条件问题")

    if is_education_verify_question(text):
        return _decision(True, tone="brief", interest="high", reason="HR询问学历核验")

    if is_leave_reason_question(text):
        return _decision(True, tone="brief", interest="high", reason="HR询问离职原因")

    if is_availability_question(text):
        return _decision(True, tone="brief", interest="high", reason="HR询问到岗时间")

    if is_interview_question(text):
        return _decision(True, tone="brief", interest="high", reason="HR询问面试相关")

    greetings = ("你好", "您好", "hi", "hello", "嗨", "在吗", "在吗？", "在不在", "在不在？")
    if lower in greetings:
        return _decision(True, tone="friendly", interest="low", reason="HR打招呼")

    # 明显问题或面试/薪资/项目类内容不跳过，交给生成器回答。
    if any(hint in lower for hint in ("?", "？", "面试", "薪资", "期望", "到岗", "项目", "经验", "技术", "方便")):
        return _decision(True, tone="professional", interest="high", reason="HR提出了需要回应的问题")

    return None


DECISION_SYSTEM_PROMPT = """你是BOSS直聘求职助手的回复决策器，只负责判断，不负责写完整回复。

你要根据HR最新消息和最近对话，决定是否需要自动回复、用什么语气、是否要触发动作。

输出严格JSON，字段如下：
{"should_reply": true/false, "tone": "brief/professional/friendly/enthusiastic/cautious", "action": "none/send_resume/send_wechat/send_phone/accept_location", "interest": "high/medium/low", "reason": "一句话原因"}

判断原则：
- HR只是说“好的/嗯嗯/收到/了解/我看一下/先这样/谢谢”等确认或结束语：should_reply=false
- HR明确拒绝、不合适、不匹配：should_reply=false, interest=low
- HR索要简历/CV/作品集：should_reply=true, action=send_resume, tone=brief
- HR索要微信或联系方式：should_reply=true, action=send_wechat, tone=brief
- HR索要电话：should_reply=true, action=send_phone, tone=brief
- HR发送“是否接受此工作地点”卡片：should_reply=false, action=accept_location
- HR问面试：优先线上面试，语气简短克制
- HR问多久到岗：回答周内可以到岗
- HR问离职原因：回答公司转型，部门撤销
- HR问是否全日制本科、学历、学信网：回答是全日制本科毕业，学信网可查
- HR问技术、项目、经验、薪资、面试、到岗、是否方便：should_reply=true
- 不要为了礼貌反复回复，宁可少回复也不要打扰HR
- 语气选择：问题严肃用professional，普通沟通用friendly，催促/确认用brief，拒绝或敏感内容用cautious
"""


def _parse_decision_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def _normalize_decision(data: dict) -> dict:
    should_reply = data.get("should_reply", data.get("reply", False))
    if isinstance(should_reply, str):
        should_reply = should_reply.strip().lower() in ("true", "yes", "1", "reply", "回复", "需要")
    return _decision(
        bool(should_reply),
        str(data.get("tone") or "brief").strip().lower(),
        str(data.get("action") or "none").strip().lower(),
        str(data.get("interest") or "low").strip().lower(),
        str(data.get("reason") or "").strip()[:120],
    )


def build_decision_context(
    conversation_id: int, hr_message: str, job_info: dict, resume_summary: str, wechat_id: str = ""
) -> str:
    user_style = get_setting("user_reply_style_profile", DEFAULT_USER_REPLY_STYLE) or DEFAULT_USER_REPLY_STYLE
    parts = [
        f"招聘方公司: {job_info.get('company', '未知')}",
        f"应聘岗位: {job_info.get('title', '未知')}",
        f"求职者本人回复语气: {user_style[:500]}",
    ]
    if job_info.get("description"):
        parts.append(f"岗位描述: {job_info.get('description', '')[:400]}")
    if resume_summary:
        parts.append(f"简历摘要: {resume_summary[:500]}")
    parts.append(f"是否配置微信: {'是' if wechat_id else '否'}")

    msgs = get_recent_messages(conversation_id, 8)
    if msgs:
        parts.append("\n最近对话:")
        for m in reversed(msgs):
            sender_label = "HR" if m["sender"] == "hr" else "我"
            parts.append(f"{sender_label}: {m['content'][:180]}")

    parts.append(f"\nHR最新消息: {hr_message}")
    return "\n".join(parts)


def decide_reply_strategy(
    conversation_id: int,
    hr_message: str,
    job_info: dict,
    resume_summary: str = "",
    wechat_id: str = "",
) -> dict:
    """Decide whether to reply, what tone to use, and whether to trigger an action."""
    rule_decision = _rule_based_decision(hr_message)
    if rule_decision:
        return rule_decision

    if get_setting("ai_reply_decision_enabled", "true") != "true":
        return _decision(True, tone=get_setting("ai_reply_style", "professional"), interest="medium", reason="自主判断关闭")

    try:
        context = build_decision_context(conversation_id, hr_message, job_info, resume_summary, wechat_id)
        messages = [
            {"role": "system", "content": DECISION_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]
        raw = llm_chat_deepseek(messages, temperature=0.1)
        return _normalize_decision(_parse_decision_json(raw))
    except Exception as e:
        # LLM decision failure should not block important HR questions, but avoid replying to very short noise.
        text = (hr_message or "").strip()
        if len(text) <= 4:
            return _decision(False, reason=f"自主判断失败，短消息保守跳过: {e}")
        return _decision(True, tone=get_setting("ai_reply_style", "professional"), interest="medium", reason=f"自主判断失败，按默认回复: {e}")


SYSTEM_PROMPT = """你是在BOSS直聘上代求职者本人进行初步沟通的求职助手。

## 核心身份
- 以求职者本人视角沟通，不要主动强调AI助手身份
- 不要说“这个工具体现技术能力”“AI代发”“由求职者本人开发”等内容
- 如果对方感兴趣，引导继续沟通即可

## 求职者背景（动态适配）
- 根据对方发布的招聘岗位来匹配你的回复侧重点
- 不要硬套一个万能模板：如果对方招的是AI产品经理，就围绕AI产品方向聊；如果招的是大模型开发，就围绕模型/工程方向聊
- 绝不要编造岗位不存在的信息，也不要提到与对方招聘岗位无关的技术领域

## 回复原则
- 2-4句话，自然真诚，不许生硬
- 围绕对方发布的岗位信息（岗位名、公司、JD）来回复
- 主动了解对方岗位的具体要求、技术栈、团队情况
- 回答技术问题时给出专业、具体的内容
- 不承诺薪资、入职时间——"这些可以后续和本人详细聊"
- 不要重复寒暄，不要每一轮都自我介绍

## 固定问题口径
- HR问面试、到公司、现场面试：优先表达“线上面试更方便”，不要直接承诺线下面试
- HR问多久到岗/什么时候入职：回答“周内可以到岗，具体时间可以配合流程再确认”
- HR问离职原因/为什么离职：回答“主要是公司业务转型，原部门撤销了，所以在看新的机会”
- HR问是否全日制本科/学历是否学信网可查：回答“是全日制本科毕业，学信网可查”
- 以上回答要短、自然，不要解释太多

## 面试处理（重要）
- 绝对不要直接同意面试或答应面试时间
- 当HR说"来面试""方便面试吗""什么时候过来"等邀请时，先引导加微信：
  "感谢邀请！方便的话可以先加微信聊聊，让求职者本人跟您沟通会更好，面试的事你们微信上直接定"
- 不要替求职者承诺面试、不要给具体时间

## 触发发送规则（重要）
系统会根据HR的消息内容自动执行以下操作，你只需要在回复中适当提及即可：

### 简历发送
- 当HR明确要求"发简历""看看简历""CV""作品集"时，系统会自动通过BOSS官方「发简历」按钮发送附件简历
- 你只需要回复"已经把简历发给您了，请查收，如果合适我们可以进一步沟通。"即可
- 绝对不要说"我这边不存储简历""没有简历文件"之类的话

### 微信交换
- 当HR说"加微信""微信聊""加个v""换微信"时，系统会自动通过BOSS官方「换微信」按钮分享求职者微信
- 你只需要回复"我把联系方式通过BOSS发您了"这类话即可
- 绝对不要在文字回复里出现"微信""WeChat""VX""微信号"这些词，BOSS会过滤掉整条消息

### 电话交换
- 当HR说"电话""手机号"时，系统会自动通过BOSS官方「换电话」按钮分享求职者电话
- 你只需要回复"我把电话通过BOSS发您了"即可

### 重要提醒
- 不要在HR没有要求的情况下主动说"已发送"
- 不要重复说"已发送"，如果之前已经发过，就不再提
- 这些操作会在你回复之前执行，所以你说"已发送"时东西确实已经发出去了

## 输出格式（严格JSON）
{"reply": "你的回复内容", "interest": "high/medium/low"}

interest 评估标准（根据完整对话判断HR当前兴趣程度）：
- high: HR问了技术细节、项目经历、面试时间、薪资期望、要了微信、表达了明确合作意向
- medium: HR配合沟通、说"方便""可以""好的""聊聊"、发了JD、问了基本情况
- low: 简单打招呼、摸底试探、回复敷衍、未表现出进一步了解的意愿"""


def _encode_wechat(wechat_id: str) -> str:
    """把微信号编码，绕开 BOSS 直聘的聊天内容过滤。"""
    if not wechat_id:
        return ""
    result = wechat_id
    result = result.replace("--", "一一")
    result = result.replace("-", "一")
    return result


def build_reply_context(
    conversation_id: int,
    hr_message: str,
    job_info: dict,
    resume_summary: str,
    wechat_id: str = "",
    user_reply_style: str = "",
) -> str:
    parts = []

    parts.append(f"招聘方公司: {job_info.get('company', '未知')}")
    parts.append(f"应聘岗位: {job_info.get('title', '未知')}")

    if user_reply_style:
        parts.append(f"求职者本人回复语气: {user_reply_style}")

    job_desc = job_info.get("description", "")
    if job_desc:
        parts.append(f"岗位描述: {job_desc[:500]}")

    if resume_summary:
        parts.append(f"我的简历摘要: {resume_summary}")

    if wechat_id:
        encoded = _encode_wechat(wechat_id)
        parts.append(f"求职者微信: {wechat_id}（BOSS会过滤微信号，实际发送时请用编码形式: {encoded}，不要发原始形式）")
    else:
        parts.append("求职者微信: 未设置")

    msgs = get_recent_messages(conversation_id, 5)
    if msgs:
        parts.append("\n最近的对话记录:")
        for m in reversed(msgs):
            sender_label = "HR" if m["sender"] == "hr" else "我"
            ai_tag = " [AI代发]" if m.get("ai_generated") else ""
            parts.append(f"  {sender_label}{ai_tag}: {m['content'][:200]}")

    parts.append(f"\nHR刚刚说: {hr_message}")
    parts.append("\n请以JSON格式输出回复和兴趣度: {\"reply\": \"...\", \"interest\": \"high/medium/low\"}")

    return "\n".join(parts)


def generate_reply(
    conversation_id: int,
    hr_message: str,
    job_info: dict,
    style: str = "professional",
    resume_summary: str = "",
    wechat_id: str = "",
) -> tuple:
    """
    根据 HR 消息生成 AI 回复和兴趣度评估。
    返回 (reply_text, interest_level) 元组，失败时返回 ("", "").
    """
    if not hr_message or len(hr_message.strip()) < 1:
        return "", ""

    hr_lower = hr_message.strip().lower()
    if should_skip_auto_reply(hr_lower):
        return "", "medium"

    if is_rejection_message(hr_lower):
        return "好的，感谢反馈，祝您招聘顺利。", "low"

    if is_resume_request(hr_lower):
        return "已经把简历发给您了，请查收，如果合适我们可以进一步沟通。", "medium"

    if is_location_accept_request(hr_lower):
        return "", "medium"

    fixed_reply, fixed_interest = common_question_reply(hr_lower)
    if fixed_reply:
        return fixed_reply, fixed_interest

    if hr_lower in ("你好", "您好", "hi", "hello", "嗨", "在吗", "在吗？", "在不在", "在不在？"):
        company = job_info.get("company", "贵公司")
        title = job_info.get("title", "相关岗位")
        desc_hint = ""
        if job_info.get("description"):
            desc_hint = f"，看了JD感觉挺对口的"
        return (
            f"您好！看到贵司在招{title}，挺感兴趣的{desc_hint}。如果岗位方向匹配，我们可以进一步沟通。",
            "low",
        )

    try:
        user_reply_style = get_setting("user_reply_style_profile", DEFAULT_USER_REPLY_STYLE) or DEFAULT_USER_REPLY_STYLE
        context = build_reply_context(
            conversation_id, hr_message, job_info, resume_summary, wechat_id, user_reply_style
        )

        style_hint = {
            "professional": "语气正式专业",
            "casual": "语气轻松友好",
            "enthusiastic": "语气热情积极",
            "brief": "语气简短克制，不要继续寒暄",
            "friendly": "语气自然友好",
            "cautious": "语气谨慎克制，避免承诺",
        }.get(style, "语气正式专业")

        messages = [
            {
                "role": "system",
                "content": (
                    SYSTEM_PROMPT
                    + f"\n\n本次回复风格: {style_hint}"
                    + f"\n\n必须贴近求职者本人的语气档案：\n{user_reply_style}"
                    + "\n\n最终回复必须像本人直接发给HR，不能像客服、销售或机器人。"
                ),
            },
            {"role": "user", "content": context},
        ]

        raw = llm_chat_deepseek(messages, temperature=0.7)
        raw = raw.strip().strip('"').strip("'").strip()

        reply = ""
        interest = ""
        try:
            parsed = json.loads(raw)
            reply = (parsed.get("reply") or parsed.get("content") or "").strip()
            interest = (parsed.get("interest") or parsed.get("level") or "").strip().lower()
        except json.JSONDecodeError:
            import re
            m = re.search(r'"reply"\s*:\s*"([^"]*)"', raw)
            if m:
                reply = m.group(1).strip()
            m2 = re.search(r'"interest"\s*:\s*"(\w+)"', raw)
            if m2:
                interest = m2.group(1).strip().lower()

        if interest not in ("high", "medium", "low"):
            interest = ""

        if not reply or len(reply) < 2:
            if not reply:
                reply = raw
            if len(reply) < 2:
                return "", ""

        if len(reply) > 300:
            reply = reply[:300] + "..."

        refusal_patterns = [
            "无法提供", "无法回答", "不能回答", "无法帮助", "爱莫能助",
            "as an AI, I cannot", "I cannot provide",
        ]
        for pattern in refusal_patterns:
            if pattern.lower() in reply.lower():
                return "", ""

        return reply, interest

    except Exception as e:
        print(f"  ⚠️ generate_reply error: {e}")
        return "", ""


def generate_greeting(
    job_title: str, company: str, template: str = "", style: str = "professional"
) -> str:
    if not template:
        template = get_setting(
            "greeting_template",
            "您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？",
        )

    greeting = template.replace("{job_title}", job_title).replace("{company}", company)

    if "{job_title}" in greeting or "{company}" in greeting:
        greeting = f"您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？"

    return greeting
