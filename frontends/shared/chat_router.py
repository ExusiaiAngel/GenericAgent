"""Transport-neutral chat routing helpers.

The router keeps short conversational messages fast while still routing
multi-step work to sub-agents.
"""

LOCAL_PREFIXES = (
    "/help", "/status", "/health", "/健康", "/体检", "/stop", "/llm",
    "/continue", "/new", "/subs", "/btw", "/mode", "/task", "/submit",
    "/todo", "/tasknow", "/now", "/run", "/runinbox", "/执行队列",
    "/tasks", "/pending", "/queue", "/队列", "/result", "/results", "/结果",
    "/alerts", "/alert", "/告警", "/doctor", "/next", "/建议",
    "/logs", "/log", "/日志",
    "/dashboard", "/dash", "/看板", "/驾驶舱",
    "/audit", "/audits", "/审计", "/审计列表",
    "/history", "/历史", "/skills", "/skill",
)

LONG_TASK_KEYWORDS = (
    "全面", "审查", "测试", "修复", "实现", "修改", "重构", "写一个", "写份", "报告",
    "计划", "部署", "搜索", "查找", "检索", "联网", "互联网", "研究", "整理", "总结", "分析", "排查", "调试", "压测",
    "进化", "优化", "升级", "安装", "配置", "迁移", "批量", "多轮",
    "系统检查", "健康检查", "运行检查", "执行检查", "体检",
    "review", "audit", "test", "fix", "implement", "deploy", "debug", "analyze",
)

PROGRESS_KEYWORDS = (
    "进展", "怎么样了", "好了没", "完成没", "完成了吗", "刚才那个", "子代理", "后台", "状态",
    "有结果了吗", "完成了没有", "任务呢", "还没好吗", "status", "progress",
)

RESEND_LAST_FOLLOWUPS = (
    "发送全文", "发全文", "完整重发", "全部重发", "报告发全",
)

FULL_RESPONSE_FOLLOWUPS = (
    "展开全文", "不要省略", "全部内容贴", "完整贴出来", "完整贴出",
)

OPERATIONAL_PATTERNS = (
    "检查", "启动", "重启", "关掉", "关闭", "停止", "保存", "写入", "删除",
    "继续执行", "接着做", "按你说的做", "照做", "现在执行", "查一下", "查看",
    "发到", "发送到", "截图", "重新跑", "重跑", "帮我改", "修好", "导出",
    "读取", "验证", "执行", "安装", "部署", "迁移",
)

EXPLANATORY_QUESTION_PATTERNS = (
    "是什么意思", "什么是", "为什么", "为何", "有什么区别", "可行吗", "会不会", "你会",
)

EXPLICIT_WORK_CUES = (
    "帮我", "请", "查一下", "检查", "给我", "把", "运行", "执行", "启动", "重启",
    "保存", "删除", "修好", "重新", "接着做", "照做",
)


def classify_chat_message(text):
    t = (text or "").strip()
    low = t.lower()
    if not t:
        return "quick_chat"
    if any(low.startswith(prefix) for prefix in LOCAL_PREFIXES):
        return "local_command"
    if any(phrase in low or phrase in t for phrase in RESEND_LAST_FOLLOWUPS):
        return "resend_last"
    if any(phrase in low or phrase in t for phrase in FULL_RESPONSE_FOLLOWUPS):
        return "context_followup"
    if len(t) >= 120:
        return "long_task"
    is_explanatory_question = any(
        phrase in low or phrase in t for phrase in EXPLANATORY_QUESTION_PATTERNS
    )
    has_explicit_work_cue = any(
        cue in low or cue in t for cue in EXPLICIT_WORK_CUES
    )
    if is_explanatory_question and not has_explicit_work_cue:
        return "quick_chat"
    if any(phrase in low or phrase in t for phrase in OPERATIONAL_PATTERNS):
        return "long_task"
    # Explicit work wins over incidental status/progress words in a larger request.
    if any(k in low or k in t for k in LONG_TASK_KEYWORDS):
        return "long_task"
    if any(k in low or k in t for k in PROGRESS_KEYWORDS):
        return "quick_tool"
    return "quick_chat"


def build_chat_style_prompt(ipc_style, route, platform="chat"):
    style_note = f"对话风格: {ipc_style}\n" if ipc_style else ""
    base = (
        f"\n\n[{platform}消息]\n"
        "这是来自聊天软件的消息。回复要像即时聊天，不要像命令行报告。\n"
        "默认先给结论，少铺垫，少贴日志，除非用户明确要求细节。\n"
        f"{style_note}"
    )
    if route == "local_command":
        return base + "这条消息应由前端本地命令处理；如果仍进入主 Agent，请给出一句简短说明。\n"
    if route == "quick_chat":
        return base + (
            "这是短聊天或轻量问答。不要派发子代理，不要调用耗时工具。"
            "直接回答，控制在 1-5 句话；不确定时先问一个澄清问题。\n"
        )
    if route == "quick_tool":
        return base + (
            "这是轻量状态或进展查询。优先直接回答或使用本地状态信息。"
            "不要启动新子代理；如果需要说明后台任务，只列最近任务状态和下一步。\n"
        )
    if route == "context_followup":
        return base + (
            "这是对上一轮结果的续接请求。当前回复会由聊天前端自动发送，"
            "不要查找 QQ 发送工具、群聊 API 或发送方式。"
            "用户要求全文、展开或重发时，只能逐字直接输出上下文中真实存在的相关原文；"
            "禁止推断或补写不存在的段落、编号、数据和结论，也不要添加前言或解释。"
            "若上一轮明确提到结果文件，可读取该文件后逐字输出；"
            "上下文和文件都没有原文时，简短说明无法恢复，绝不能编造。\n"
        )
    if route == "long_task":
        return base + (
            "这是多步或耗时任务。可以派发子代理执行实际工作。"
            "派发后立即简短告知用户：已开始、任务名、预计会主动回报、可用 /btw 查看进展。"
            "不要把内部工具日志发到 QQ。"
            "联网研究最多进行两轮搜索词改写和两轮正文抓取；若仍无相关、可验证来源，"
            "立即停止并明确回复当前网络环境无法验证，列出已尝试来源。"
            "禁止把下一步动作句当最终结果。\n"
        )
    return base + "按短聊天处理，直接给清晰回复。\n"


# Backward-compatible adapter names used by existing frontends/tests.
classify_qq_message = classify_chat_message
build_qq_style_prompt = build_chat_style_prompt
