import unittest

from agent_loop import _StuckDetector


class StuckDetectorTests(unittest.TestCase):
    def test_repeated_tool_with_substantial_response_growth_is_not_no_progress(self):
        detector = _StuckDetector()
        call = [{"tool_name": "file_read", "args": {"path": "memory/a.md"}}]

        warnings = []
        for response_length in (100, 1000, 5000):
            detector.record(call, response_length)
            warnings.append(detector.check(response_length))

        self.assertIsNone(warnings[-1])

    def test_repeated_tool_with_flat_response_is_no_progress(self):
        detector = _StuckDetector()
        call = [{"tool_name": "file_read", "args": {"path": "memory/a.md"}}]

        for response_length in (100, 105, 110):
            detector.record(call, response_length)
            warning = detector.check(response_length)

        self.assertIn("无进展", warning)


if __name__ == "__main__":
    unittest.main()
