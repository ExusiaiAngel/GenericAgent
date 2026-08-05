import unittest


class TaskModeStartupTests(unittest.TestCase):
    def test_task_mode_disables_watchdog_and_ipc(self):
        from agentmain import runtime_service_policy

        self.assertEqual(
            runtime_service_policy(task_mode=True),
            {"watchdog": False, "ipc": False},
        )

    def test_service_mode_keeps_watchdog_and_ipc(self):
        from agentmain import runtime_service_policy

        self.assertEqual(
            runtime_service_policy(task_mode=False),
            {"watchdog": True, "ipc": True},
        )

