"""邮件截图页不得启动首页数据加载。"""
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class TestMailRenderPage(TestCase):
    def test_mail_renderer_uses_minimal_page_mode(self):
        source = (ROOT / "stockfu/services/mail.py").read_text()
        self.assertIn('?mail_render=1', source)

    def test_web_skips_load_all_in_mail_mode(self):
        page = (ROOT / "stockfu/web/index.html").read_text()
        self.assertIn("has('mail_render')) loadAll();", page)

    def test_empty_mail_data_still_renders_a_market_page(self):
        page = (ROOT / "stockfu/web/index.html").read_text()
        self.assertIn("if(!sortedSec.length&&!pages.length)", page)
        self.assertIn("${shareHead(d)}${shareTail()}", page)

    def test_renderer_times_out_when_a_page_cannot_be_built(self):
        source = (ROOT / "stockfu/services/mail.py").read_text()
        self.assertIn("邮件分享卡片未生成页面", source)
