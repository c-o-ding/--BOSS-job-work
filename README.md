<div align="center">

# BOSS-job-work

**BOSS 直聘自动化求职控制台，包含 Web 控制台、CLI、岗位评分和 AI 回复能力。**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white&style=flat-square)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](LICENSE)
[![CLI](https://img.shields.io/badge/CLI-boss--job--work-blue.svg?style=flat-square)](#cli-命令)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](https://github.com/c-o-ding/--BOSS-job-work/pulls)

</div>

> 这是一个本地运行的 BOSS 直聘求职辅助项目。它把岗位搜索、筛选、待投递池、批量投递、聊天同步、自动回复、岗位评分、环境自检等能力放到一个本地控制台里，适合个人求职时使用。

## 重要说明

- 本项目默认本地运行，不依赖云端 SaaS。
- 不使用 AI 时，核心能力如启动浏览器、扫码登录、岗位搜索、待投递池、批量投递、消息同步都可以使用。
- 只有在启用 AI 自动回复、AI 岗位分析、AI 风格化回复时，才需要额外配置兼容 OpenAI 接口的模型服务。
- 项目涉及浏览器自动化，请先阅读 [DISCLAIMER.md](DISCLAIMER.md)。

## 当前能力

### 1. 岗位搜索与筛选

- 支持多关键词搜索，关键词可用逗号分隔，例如 `python,linux,ai agent`
- 支持城市筛选
- 支持最低薪资筛选，例如最低 `8K`
- 支持福利关键词筛选，例如 `双休,五险一金`
- 支持屏蔽词，例如 `专家,经理,商务`
- 搜索结果可写入本地待投递池
- 支持清空待投递池

### 2. 岗位评分与状态流转

- 本地岗位评分体系，输出 `fit_score / fit_level / fit_recommendation`
- 推荐等级支持 `A / B / C / D / F`
- 推荐动作支持 `进行 / 复核 / 跳过`
- 支持岗位去重，优先用岗位 URL 去重
- 支持待投递、已投递、已回复、已跳过、失败等状态流转
- 支持重新评分和最低投递分阈值

### 3. 投递与批量处理

- 支持单个投递
- 支持待投递池批量投递
- 支持扫描当前搜索页并批量投递
- 支持每日投递上限
- 支持投递前自动生成招呼语
- 投递过程中会暂停聊天监控，避免浏览器任务冲突

### 4. 聊天同步与自动回复

- 支持同步 BOSS 会话到本地
- 支持同步单个会话的全部消息
- 支持聊天监控开启 / 暂停 / 恢复
- 支持全局自动回复开关
- 支持单会话自动回复开关
- 支持手动发送消息
- 支持根据消息内容决定是否回复
- 支持部分卡片类操作，例如同意简历、地点接受等页面动作

### 5. 本地控制台与 CLI

- Web 控制台适合日常操作
- CLI 适合脚本化调用和 AI Agent 集成
- CLI 默认输出 JSON，方便接入自动化流程
- 提供 `doctor` 自检能力，检查浏览器、登录态、AI 配置和数据一致性

## 技术结构

```text
浏览器 UI / Dashboard
        │
        ├── FastAPI API + WebSocket
        │       ├── boss_app.py
        │       ├── boss_automation.py
        │       ├── boss_firefox.py
        │       ├── boss_replier.py
        │       ├── boss_job_intelligence.py
        │       └── boss_state.py
        │
        ├── SQLite 本地数据
        │
        └── Playwright + Firefox 持久化用户目录
```

核心模块：

- `boss_app.py`：FastAPI 后端和 WebSocket 推送
- `boss_automation.py`：浏览器自动化、投递、会话同步、聊天监控
- `boss_firefox.py`：BOSS 页面导航、搜索、页面选择器和持久化 profile
- `boss_replier.py`：招呼语和自动回复生成
- `boss_job_intelligence.py`：岗位评分、去重、profile、doctor 自检
- `boss_state.py`：SQLite 持久化、会话和岗位状态
- `boss_job_work_cli/`：CLI 客户端
- `static/dashboard.html`：单页控制台

## 运行环境

推荐环境：

- Windows 10 / 11
- Python 3.10 到 3.12
- Firefox
- Playwright

安装依赖：

```bash
git clone https://github.com/c-o-ding/--BOSS-job-work.git BOSS-job-work
cd BOSS-job-work

python -m venv .venv
.venv\Scripts\activate

pip install -U pip
pip install -e .
playwright install firefox
```

也可以直接使用项目里的启动脚本：

- `start-boss-job-work.bat`
- `stop-boss-job-work.bat`

## 快速开始

### 方式一：脚本启动

双击或命令行运行：

```bat
start-boss-job-work.bat
```

脚本会：

1. 启动本地 API 服务，默认端口 `8010`
2. 检查服务健康状态
3. 打开本地控制台 `http://127.0.0.1:8010`
4. 可选自动触发“启动浏览器 + 启动监控”

停止服务：

```bat
stop-boss-job-work.bat
```

### 方式二：手动启动

```bash
python boss_app.py --port 8010
```

然后在浏览器打开：

```text
http://127.0.0.1:8010
```

### 首次使用流程

1. 打开控制台
2. 点击“启动浏览器”
3. 在弹出的 Firefox 中扫码登录 BOSS 直聘
4. 回到控制台，在设置页调整：
   - 默认城市
   - 每日投递上限
   - 最低薪资
   - 屏蔽关键词
   - AI 自动回复开关
   - AI 模型配置
5. 在岗位搜索页输入关键词开始搜索

## 是否必须配置 API

不是必须。

### 不配置 API 也能用的功能

- 启动浏览器
- 扫码登录
- 岗位搜索
- 福利 / 薪资 / 城市 / 屏蔽词筛选
- 待投递池管理
- 单个投递 / 批量投递
- 会话同步
- 手动发送消息
- doctor 自检

### 需要配置 API 的功能

- AI 自动回复
- AI 风格化回复
- AI 岗位分析
- 更智能的回复判断

兼容方式：

- DeepSeek
- OpenRouter
- 其他兼容 OpenAI Chat Completions 的接口
- 自定义 Base URL + Model

## 控制台功能说明

### 岗位搜索页

- 关键词支持逗号分隔
- 可以按城市、最低薪资、福利、屏蔽词搜索
- 可以对结果按状态和推荐动作筛选
- 支持一键搜索、多关键词累计结果
- 支持清空待投递池
- 支持从搜索结果直接投递

### 投递记录页

- 查看已保存岗位
- 查看状态流转
- 查看投递统计
- 对待投递岗位批量执行投递

### 聊天页

- 同步 BOSS 会话到本地
- 查看本地缓存消息
- 打开单个会话
- 手动发送消息
- 开启 / 暂停单会话 AI 回复
- 开启 / 关闭全局自动回复
- 开启 / 暂停聊天监控

### 设置页

常用设置包括：

- 默认城市
- 每日投递上限
- 最低薪资门槛
- 屏蔽关键词
- 候选人 profile
- 招呼语模板
- 简历摘要
- AI 回复风格
- AI 自动回复开关
- AI Base URL / API Key / Model

## CLI 命令

安装完成后会提供 `boss-job-work` 命令。

```bash
boss-job-work status
boss-job-work doctor
boss-job-work search "python,linux" --city 深圳 --salary-min 8 --exclude "专家,经理"
boss-job-work jobs --status pending --limit 20
boss-job-work apply-batch
boss-job-work conversations
boss-job-work chat 1
boss-job-work send 1 --msg "您好，我已补充信息。"
boss-job-work server --start --port 8010
```

主要命令：

| 命令 | 说明 |
|---|---|
| `boss-job-work search` | 搜索岗位 |
| `boss-job-work status` | 查看浏览器和监控状态 |
| `boss-job-work stats` | 查看统计数据 |
| `boss-job-work jobs` | 查询本地岗位记录 |
| `boss-job-work apply` | 投递单个岗位 |
| `boss-job-work apply-batch` | 批量投递待投递岗位 |
| `boss-job-work scan` | 扫描当前搜索结果页 |
| `boss-job-work scan-apply` | 扫描并立即批量投递 |
| `boss-job-work conversations` | 查看会话列表 |
| `boss-job-work chat` | 查看单个会话消息 |
| `boss-job-work send` | 手动发送消息 |
| `boss-job-work doctor` | 执行环境和数据自检 |
| `boss-job-work login` | 重新扫码登录 |
| `boss-job-work server` | 启停本地后端服务 |

CLI 输出统一是 JSON 包装格式，便于脚本和 Agent 解析。

## 数据与目录

本项目的数据主要保存在本地：

- `.boss_profile/`：浏览器持久化目录，包含登录态、cookie、会话缓存
- SQLite：岗位、会话、消息、设置等本地数据
- `logs/`：服务日志

这些目录不建议提交到公开仓库。

## Doctor 自检

项目内置 `doctor` 能力，会检查：

- Python 环境
- 浏览器是否已启动
- 登录态是否可用
- AI 配置是否完整
- 岗位 profile 配置是否合理
- 本地数据的一致性

运行方式：

```bash
boss-job-work doctor
```

## 常见问题

### 1. 浏览器启动失败

先确认：

- 已执行 `playwright install firefox`
- 没有残留 Firefox 进程占用 profile
- 没有同时重复启动多个实例

如果出现 Firefox 已运行但无响应，先关闭旧 Firefox 进程，再重试。

### 2. 控制台提示登录失效

这通常是 BOSS 登录态失效或页面触发了验证。重新扫码登录即可。

### 3. 搜索和聊天监控冲突

当前实现会在搜索、批量投递等重操作期间暂停聊天监控，结束后恢复。这是为了避免一个浏览器同时做两种冲突动作。

### 4. 自动回复没有生效

检查：

- 全局自动回复是否开启
- 当前会话自动回复是否开启
- 是否配置了 AI 模型
- 页面当前是否处于验证码、异常弹窗、登录失效状态

### 5. 启动脚本卡在健康检查

先看：

- `logs/server.out.log`
- `logs/server.err.log`

必要时直接运行：

```bash
python boss_app.py --port 8010
```

## 开发说明

安装开发依赖：

```bash
pip install -e .[dev]
```

建议开发流程：

1. 修改后先跑本地控制台
2. 再测试 CLI
3. 再测试浏览器实际流程
4. 最后检查 `doctor`

## 免责声明

本项目只适合个人求职辅助使用，不适合大规模商用抓取或批量营销。详细条款见 [DISCLAIMER.md](DISCLAIMER.md)。
