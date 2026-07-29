"""邮件 MIME 结构：正文内嵌图片，附件作为无法显示 CID 时的保底。"""
from __future__ import annotations

from datetime import date
from email import message_from_string
from unittest import TestCase, mock


class _FakeSmtp:
    sent: str | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def login(self, *_args):
        pass

    def sendmail(self, *_args):
        self.__class__.sent = _args[2]


class TestMailMessage(TestCase):
    def test_embeds_and_attaches_each_image(self):
        from stockfu.services import mail

        _FakeSmtp.sent = None
        with mock.patch("stockfu.config.get_smtp_user", return_value="sender@example.com"), \
             mock.patch("stockfu.config.get_smtp_pass", return_value="token"), \
             mock.patch("stockfu.config.get_mail_to", return_value="to@example.com"), \
             mock.patch("stockfu.config.get_smtp_from", return_value=""), \
             mock.patch("stockfu.config.get_smtp_host", return_value="smtp.example.com"), \
             mock.patch("stockfu.config.get_smtp_port", return_value=465), \
             mock.patch("stockfu.services.snapshot.latest_trade_date", return_value=date(2026, 7, 29)), \
             mock.patch.object(mail.smtplib, "SMTP_SSL", _FakeSmtp):
            result = mail.send_card_email([b"png-page"])

        self.assertTrue(result["ok"])
        message = message_from_string(_FakeSmtp.sent or "")
        self.assertEqual(message.get_content_subtype(), "related")
        parts = list(message.walk())
        self.assertTrue(any(p.get_content_type() == "multipart/alternative" for p in parts))
        images = [p for p in parts if p.get_content_type() == "image/png"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["Content-ID"], "<page-0>")
        self.assertIn("inline", images[0]["Content-Disposition"])
        self.assertIn("attachment", images[1]["Content-Disposition"])
