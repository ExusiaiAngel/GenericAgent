import unittest

from ga import _parse_bing, _relevance_score, _select_relevant_results


BING_HTML = """
<li class="b_algo">
  <a href="https://example.com/breadcrumb">example.com</a>
  <h2><a href="https://www.nintendo.com/xenoblade-news">Xenoblade new project news</a></h2>
  <div class="b_caption"><p>Monolith Soft discusses a new Xenoblade project.</p></div>
</li>
"""


class WebSearchQualityTests(unittest.TestCase):
    def test_bing_parser_uses_h2_result_link(self):
        rows = _parse_bing(BING_HTML, "Xenoblade new project", 5)
        self.assertEqual(rows[0]["title"], "Xenoblade new project news")
        self.assertEqual(rows[0]["url"], "https://www.nintendo.com/xenoblade-news")

    def test_irrelevant_results_are_rejected(self):
        rows = [{
            "title": "向日葵远程控制教程",
            "url": "https://example.com/sunflower",
            "snippet": "手机投屏与远程桌面",
            "backend": "bing",
        }]
        selected = _select_relevant_results(
            "异度之刃 新作 2026 最新消息",
            rows,
            max_results=5,
        )
        self.assertEqual(selected, [])

    def test_old_franchise_page_is_rejected_for_new_game_query(self):
        rows = [{
            "title": "Xenoblade Chronicles encyclopedia",
            "url": "https://example.com/xenoblade",
            "snippet": "The original game launched on Wii in 2010.",
            "backend": "bing",
        }]
        self.assertEqual(
            _select_relevant_results(
                "Xenoblade Chronicles new game 2026 announcement",
                rows,
                max_results=5,
            ),
            [],
        )

    def test_year_alone_does_not_make_unrelated_chinese_result_relevant(self):
        rows = [{
            "title": "豆包中文版官方网站",
            "url": "https://example.com/doubao",
            "snippet": "Jun 21, 2026 智能问答与联网检索服务",
            "backend": "bing",
        }]
        self.assertEqual(
            _select_relevant_results(
                "异度之刃 新作 2026 最新消息",
                rows,
                max_results=5,
            ),
            [],
        )

    def test_relevant_result_scores_above_zero(self):
        row = {
            "title": "Xenoblade Chronicles new project",
            "url": "https://www.nintendo.com/xenoblade",
            "snippet": "Monolith Soft announces a 2027 release",
        }
        self.assertGreater(
            _relevance_score("Xenoblade Chronicles new game", row),
            0,
        )
