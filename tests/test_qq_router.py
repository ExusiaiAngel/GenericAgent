import unittest

from frontends.shared.chat_router import classify_qq_message, build_qq_style_prompt


class QQRouterTests(unittest.TestCase):
    def test_quick_chat_receives_only_safe_read_or_question_tools(self):
        from frontends.shared.task_protocol import tool_schema_for_route

        schema = [
            {"type": "function", "function": {"name": name}}
            for name in ("file_read", "ask_user", "code_run", "spawn_subagent", "qq_group")
        ]
        quick = tool_schema_for_route(schema, "quick_chat")
        long_task = tool_schema_for_route(schema, "long_task")

        self.assertEqual(
            {item["function"]["name"] for item in quick},
            {"file_read", "ask_user"},
        )
        self.assertEqual(len(long_task), len(schema))

    def test_local_commands_stay_local(self):
        self.assertEqual(classify_qq_message("/status"), "local_command")
        self.assertEqual(classify_qq_message("/health"), "local_command")
        self.assertEqual(classify_qq_message("/体检"), "local_command")
        self.assertEqual(classify_qq_message("/btw 刚才那个"), "local_command")
        self.assertEqual(classify_qq_message("/task 生成明天的简报"), "local_command")
        self.assertEqual(classify_qq_message("/tasknow 生成明天的简报"), "local_command")
        self.assertEqual(classify_qq_message("/run"), "local_command")
        self.assertEqual(classify_qq_message("/tasks"), "local_command")
        self.assertEqual(classify_qq_message("/result"), "local_command")
        self.assertEqual(classify_qq_message("/alerts"), "local_command")
        self.assertEqual(classify_qq_message("/doctor"), "local_command")
        self.assertEqual(classify_qq_message("/next"), "local_command")
        self.assertEqual(classify_qq_message("/建议"), "local_command")
        self.assertEqual(classify_qq_message("/logs inbox 20"), "local_command")
        self.assertEqual(classify_qq_message("/dashboard"), "local_command")
        self.assertEqual(classify_qq_message("/audit"), "local_command")
        self.assertEqual(classify_qq_message("/audits"), "local_command")

    def test_short_chat_does_not_force_subagent(self):
        self.assertEqual(classify_qq_message("在吗"), "quick_chat")
        self.assertEqual(classify_qq_message("这个是什么意思"), "quick_chat")

    def test_long_work_routes_to_subagent(self):
        msg = "全面审查 GenericAgent 的 QQ 模块，测试子代理完成回报，并写一份修复计划"
        self.assertEqual(classify_qq_message(msg), "long_task")

    def test_self_evolution_routes_to_long_task(self):
        self.assertEqual(classify_qq_message("进行四轮自进化"), "long_task")
        self.assertEqual(classify_qq_message("优化并升级现有工具"), "long_task")

    def test_system_health_check_wording_routes_to_long_task(self):
        for message in (
            "运行系统检查",
            "执行健康检查",
            "给 GenericAgent 做一次体检",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_qq_message(message), "long_task")

    def test_full_response_followup_has_a_dedicated_direct_reply_route(self):
        from frontends.shared.task_protocol import turns_for_route

        for message in (
            "展开全文",
            "不要省略，把全部内容贴出来",
            "把代码完整贴出来",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_qq_message(message), "context_followup")
        for message in ("发送全文", "发全文", "完整重发一次", "把刚才的报告发全"):
            with self.subTest(message=message):
                self.assertEqual(classify_qq_message(message), "resend_last")
        self.assertEqual(turns_for_route("context_followup"), 8)
        prompt = build_qq_style_prompt("群聊", "context_followup")
        self.assertIn("当前回复会由聊天前端自动发送", prompt)
        self.assertIn("直接输出", prompt)
        self.assertIn("不要查找 QQ 发送工具", prompt)
        self.assertIn("逐字", prompt)
        self.assertIn("禁止推断或补写", prompt)

    def test_short_operational_requests_are_not_treated_as_chat(self):
        for message in (
            "检查一下日志",
            "启动QQ服务",
            "重启一下GenericAgent",
            "关掉NapCat",
            "把这个保存到文件",
            "删除刚才的临时文件",
            "继续执行",
            "接着做",
            "按你说的做",
            "照做",
            "现在执行",
            "查一下为什么失败",
            "把结果发到群里",
            "给我截图",
            "重新跑一遍",
            "帮我改一下",
            "修好它",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_qq_message(message), "long_task")

    def test_explanatory_questions_do_not_trigger_work_by_keyword_alone(self):
        for message in (
            "报告是什么意思",
            "测试是什么意思",
            "为什么配置这么复杂",
            "你会搜索吗",
            "什么是重构",
            "分析和总结有什么区别",
            "修复和重构有什么区别",
            "这个计划可行吗",
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_qq_message(message), "quick_chat")

    def test_internet_research_wording_routes_to_long_task(self):
        self.assertEqual(
            classify_qq_message("在互联网上查找有关异度之刃新作的消息，写个总结"),
            "long_task",
        )

    def test_progress_text_routes_to_quick_tool(self):
        self.assertEqual(classify_qq_message("刚才那个好了没"), "quick_tool")
        self.assertEqual(classify_qq_message("现在进展怎么样"), "quick_tool")
        self.assertEqual(classify_qq_message("有结果了吗"), "quick_tool")
        self.assertEqual(classify_qq_message("完成了没有"), "quick_tool")
        self.assertEqual(classify_qq_message("任务呢"), "quick_tool")
        self.assertEqual(classify_qq_message("还没好吗"), "quick_tool")

    def test_prompt_for_quick_chat_blocks_subagent(self):
        prompt = build_qq_style_prompt("群聊", "quick_chat")
        self.assertIn("不要派发子代理", prompt)
        self.assertIn("直接回答", prompt)

    def test_prompt_for_long_task_allows_subagent(self):
        prompt = build_qq_style_prompt("群聊", "long_task")
        self.assertIn("可以派发子代理", prompt)
        self.assertIn("立即简短告知用户", prompt)

    def test_representative_63_message_intent_matrix(self):
        expected_by_route = {
            "quick_chat": (
                "在吗", "你好", "谢谢", "好的", "你是谁", "1+1等于几",
                "这个是什么意思", "简单解释一下 FTS5", "你觉得QQ好用吗", "晚安",
                "报告是什么意思", "测试是什么意思", "为什么配置这么复杂", "你会搜索吗",
                "什么是重构", "分析和总结有什么区别", "修复和重构有什么区别",
                "这个计划可行吗",
            ),
            "quick_tool": (
                "刚才那个好了没", "现在进展怎么样", "任务完成了吗", "后台状态如何",
                "子代理怎么样了", "刚才那个进展呢", "有结果了吗", "完成了没有",
                "任务呢", "还没好吗",
            ),
            "local_command": (
                "/status", "/health", "/logs inbox 20", "/history 系统检查",
                "/task 生成日报", "/result",
            ),
            "resend_last": (
                "把刚才的报告发全", "发送全文", "完整重发一次",
            ),
            "context_followup": (
                "展开全文", "不要省略，把全部内容贴出来", "把代码完整贴出来",
            ),
            "long_task": (
                "运行系统检查", "检查一下日志", "启动QQ服务", "重启一下GenericAgent",
                "关掉NapCat", "把这个保存到文件", "删除刚才的临时文件", "读取完整报告",
                "继续执行", "接着做", "按你说的做", "照做", "现在执行",
                "查一下为什么失败", "把结果发到群里", "给我截图", "导出报告",
                "重新跑一遍", "验证修复结果", "搜索最新资料", "分析这个仓库",
                "帮我改一下", "修好它",
            ),
        }
        self.assertEqual(sum(map(len, expected_by_route.values())), 63)
        for expected, messages in expected_by_route.items():
            for message in messages:
                with self.subTest(expected=expected, message=message):
                    self.assertEqual(classify_qq_message(message), expected)


if __name__ == "__main__":
    unittest.main()
