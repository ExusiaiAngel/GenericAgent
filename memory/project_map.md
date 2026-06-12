# GenericAgent Project Map

## Primary Entrypoints

| 入口 | 路径 | 说明 |
|:----|:----|:------|
| **主agent循环** | `agentmain.py` | 命令行主入口，加载插件+memory+tool schema，启动 agent 循环 |
| **Agent循环核心** | `agent_loop.py` | 紧凑自主执行循环 (~100行) |
| **工具层** | `ga.py` | 595行，定义所有 agent 工具（code_run/file_read/web_scan 等） |
| **CLI 命令** | `ga_cli/cli.py` | 安装为 `ga` 命令，分发子命令 |
| **桌面启动** | `launch.pyw` | 桌面启动界面 |
| **Hub 启动** | `hub.pyw` | 多 agent hub 界面 |

## Core Runtime

| 模块 | 路径 | 说明 |
|:----|:----|:------|
| **LLM 集成** | `llmcore.py` | LLM 会话和模型集成核心 (1063行) |
| **浏览器控制** | `TMWebDriver.py` | 通过 Chrome/Edge 扩展接管用户浏览器 (286行，已打便携Python补丁) |
| **HTML 简化** | `simphtml.py` | HTML/browser 工具 |
| **配置** | `mykey.py` | API 密钥和配置（引用不读取内容） |
|| **配置模板** | `mykey_template.py` / `mykey_template_en.py` | mykey 配置模板 (425行 / 76行) |
| **插件钩子** | `plugins/hooks.py` | 插件扩展点 |

## Memory System

| 层 | 路径 | 用途 |
|:---|:----|:------|
| **L0 Meta-SOP** | `memory/memory_management_sop.md` | 记忆管理元规范 |
| **L1 索引** | `memory/global_mem_insight.txt` | 紧凑启动索引，路由到所有模块 (30行) |
| **L2 事实** | `memory/global_mem.txt` | 稳定环境事实 |
| **L3 SOPs** | `memory/*_sop.md` | 可复用工作流 (含新沉淀: memory_index_hygiene, launch_readiness_check, web_research) |
| **L4 历史** | `memory/L4_raw_sessions/` | 原始会话归档，用于长周期回忆 (`salient_mining.py` 自动化 → scheduler 每10分钟 → L2) |

### 关键配置/边界文档
- `personal_bootstrap_profile.md` — 用户环境/偏好/安全边界
- `cold_start_task_queue.md` — 首周任务队列 + SOP 结晶标准
- `sandbox_policy.md` — 写入权限边界（仅 sandbox 预批准）
- `personal_supervision_sop.md` — 监督 SOP
- `launch_readiness_report.md` — 启动就绪报告

## Reflective / Autonomy Modules (`reflect/`)

| 模块 | 说明 |
|:----|:------|
| `goal_mode.py` | 时间预算自驱动目标循环 |
| `scheduler.py` | 定时执行支持 |
| `autonomous.py` | 自主操作模式 |
| `checklist_master.py` | 检查清单主控 |
| `agent_team_worker.py` | Agent 团队协作工作器 |

## Frontends (`frontends/`)

| 前端 | 类型 |
|:----|:-----|
| `tuiapp_v2.py` | Textual TUI |
| `qtapp.py` | Qt UI |
| `conductor.py` | 子 agent 编排前端 |
| `conductor_im_plugins/` | Conductor IM 插件 |
| `chatapp_common.py` | 聊天应用公共模块 |
| `btw_cmd.py` | 后台任务工具 |
| `continue_cmd.py` | 继续命令工具 |
| `cost_tracker.py` | 成本追踪 |
| `tgapp.py` | Telegram 机器人 |
| `wechatapp.py` | 微信集成 |
| `dingtalkapp.py` | 钉钉集成 |
| `wecomapp.py` | 企业微信集成 |
| `qqapp.py` | QQ 集成 |

## Plugin Hooks (`plugins/`)

| 文件 | 说明 |
|:----|:------|
| `hooks.py` | 插件钩子原语 (discover_and_load) |
| `langfuse_tracing.py` | 可选追踪集成 |

## Specialized Utilities (`memory/`)

| 工具 | 说明 |
|:----|:------|
| `ljqCtrl.py` | 键盘/鼠标/截图控制 (ljqCtrl_sop.md) |
| `adb_ui.py` | 移动设备 ADB 控制 |
| `ocr_utils.py` | OCR 工具 |
| `procmem_scanner.py` | 进程内存扫描器 |
| `keychain.py` | 密钥链管理 |
| `checklist_helper.py` | 检查清单辅助 |
| `ui_detect.py` | 界面检测 |
| `computer_use.md` | GUI 操作指南 |

## Docs & Assets

| 路径 | 说明 |
|:----|:------|
| `docs/GETTING_STARTED.md` | 入门指南 |
| `docs/SETUP_FEISHU.md` | 飞书设置 |
| `docs/installation.md/en/zh` | 安装文档 |
| `docs/superpowers/` | 超级能力文档 |
| `assets/tmwd_cdp_bridge/` | CDP 桥 Chrome/Edge 扩展 |
| `assets/supergrok_proxy.py` | SuperGrok 代理 |
| `assets/tools_schema.json/.cn` | 工具 Schema |
| `assets/sys_prompt.txt/.en` | 系统提示词 |
| `assets/configure_mykey.py` | mykey 配置工具 |
| `assets/GenericAgent_Technical_Report.pdf` | 技术报告 |

## 当前启动命令

```bash
python agentmain.py
```

## 冷启动进度 (2026-06-08)

| # | 任务 | 状态 |
|:-|:-----|:----:|
| 1 | ~~Project Map~~ | ✅ 完成 |
| 2 | Memory Index Hygiene | ✅ 完成 |
| 3 | Launch Readiness Check | ✅ 完成 |
| 4 | First Reusable Local Skill | ✅ 完成 |
| 5 | Controlled Browser Workflow | ✅ 完成 |
| 6 | System Environment Audit | ✅ 完成 |
| 7 | Project Dependency Check | ✅ 完成 |
| 8 | Git Workspace Hygiene | ✅ 完成 |
| 9 | Unified Health Check | ✅ 完成 |
| 10 | System Process Monitor | ✅ 完成 |
| 11 | Backup Verification | ✅ 完成 |
| 12 | File Organization Analysis | ✅ 完成 |
| 13 | Daily Brief | ✅ 完成 |
| 14 | Config Audit | ✅ 完成 |
| 15 | Network Monitor | ✅ 完成 |

**新增能力**:
- **CLAUDE.md** — 项目根级引导配置，指向 bootstrap/L0/安全规则/常用命令/L4管线
- **L4 自动挖掘管线** — `salient_mining.py` → `reflect/scheduler.py` 每10分钟增量运行 → `global_mem.txt` L2 事实更新。状态文件: `history_insight/` (processed_session + activity_knowledge + emotional_events)
- **health_check_sop.md** — 每日健康检查结晶SOP，组合 env_audit + dep_check + git_hygiene 三个 SOP，一键生成仪表盘报告。触发词："每日健康检查" / "health check"。
- **reusable_task_runner_sop.md** — 可复用任务运行器 SOP，沉淀 `--task --once` 模式（经10+轮次验证），标准化 input.txt 协议 + task_watchdog.py 监督。
- **web_search + web_fetch** — 11 工具链，多后端搜索(DuckDuckGo→Bing)，curl_cffi Chrome TLS 伪装，agent 可独立上网调研。
- **config_check.py** — 18 项自检工具，启动时自动运行(env/imports/config/tools/memory/system)。
- **new_machine_setup_sop.md** — 5 步新机就绪，从零到 GenericAgent 启动。
- **next_phase_goals.md** — Phase 2 进化路线图：5 域 16 目标, 3 阶段时间线。

### 依赖修复备忘
- **TMWebDriver.py:3-5**: 便携Python site-packages兼容补丁
- **agentmain.py:363-377**: done.json rounds 计数 BUG 修复 (3处)
- **web_research_sop.md**: 路径解析规则 (绝对路径要求，--task CWD 在 temp/)

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |

