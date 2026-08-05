"""Shared route budgets and truthful task terminal-state normalization."""

ROUTE_MAX_TURNS = {
    "quick_chat": 4,
    "quick_tool": 6,
    "context_followup": 8,
    "long_task": 24,
}

ROUTE_ALLOWED_TOOLS = {
    "quick_chat": frozenset({"file_read", "ask_user"}),
    "quick_tool": frozenset(),
    "context_followup": frozenset({"file_read", "ask_user"}),
}


def turns_for_route(route):
    return ROUTE_MAX_TURNS.get(route, ROUTE_MAX_TURNS["quick_chat"])


def tool_schema_for_route(schema, route):
    """Enforce route permissions on the schema actually sent to the model."""
    if route not in ROUTE_ALLOWED_TOOLS:
        return list(schema or [])
    allowed = ROUTE_ALLOWED_TOOLS[route]
    return [
        tool for tool in (schema or [])
        if (tool.get("function") or {}).get("name") in allowed
    ]


def _terminal(state, *, text="", error="", question="", candidates=None):
    return {
        "state": state,
        "text": str(text or ""),
        "error": str(error or ""),
        "question": str(question or ""),
        "candidates": [str(value) for value in (candidates or [])],
    }


def terminal_from_runner(runner_result, full_text, max_turns):
    result = runner_result or {"result": "MAX_TURNS_EXCEEDED"}
    state = result.get("result", "MAX_TURNS_EXCEEDED")
    data = result.get("data")

    if state == "MAX_TURNS_EXCEEDED":
        return _terminal(
            "max_turns",
            error=f"任务在 {max_turns} 个模型回合后仍未完成，已安全停止。",
        )

    if state == "EXITED" and isinstance(data, dict):
        if data.get("status") == "INTERRUPT" and data.get("intent") == "HUMAN_INTERVENTION":
            prompt = data.get("data") or {}
            return _terminal(
                "needs_input",
                question=prompt.get("question") or "请确认是否继续。",
                candidates=prompt.get("candidates") or [],
            )
        if data.get("status") == "STOPPED":
            return _terminal("stopped", error="任务已停止。")
        return _terminal("stopped", error="任务在生成最终答案前停止。")

    if state == "CURRENT_TASK_DONE":
        text = str(full_text or "").strip()
        if not text:
            return _terminal("failed", error="模型未生成可见的最终答案。")
        return _terminal("completed", text=text)

    return _terminal("failed", error=f"未知任务终态：{state}")
