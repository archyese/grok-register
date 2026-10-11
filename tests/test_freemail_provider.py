"""Freemail 临时邮箱接入回归测试。

接口契约参考 grok-register-back：
- GET /api/generate            -> {"email": "..."}，无独立凭证，邮箱地址即收件箱标识
- GET /api/emails?mailbox=...  -> 邮件列表，条目可能带 verification_code 字段
- GET /api/email/{id}/download -> 原始 EML 文本
"""

import base64
import unittest
from unittest.mock import Mock, patch

import app_config
import mail_freemail
import mail_service


class DummyResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


FREEMAIL_CONFIG = {
    "email_provider": "freemail",
    "freemail_domain": "mail.example.test",
    "freemail_token": "token-123",
}


def _eml_with_body(body_html):
    encoded = base64.b64encode(body_html.encode("utf-8")).decode("ascii")
    return (
        "From: noreply@x.ai\n"
        "To: user@example.test\n"
        "Subject: =?utf-8?B??=\n"
        "Content-Type: text/html; charset=utf-8\n"
        "Content-Transfer-Encoding: base64\n"
        "\n"
        + encoded
    )


class FreemailConfigTests(unittest.TestCase):
    def test_normalize_api_base_accepts_bare_domain_and_full_url(self):
        self.assertEqual(
            mail_freemail.freemail_normalize_api_base("mail.example.com"),
            "https://mail.example.com",
        )
        self.assertEqual(
            mail_freemail.freemail_normalize_api_base("https://mail.example.com/"),
            "https://mail.example.com",
        )
        self.assertEqual(mail_freemail.freemail_normalize_api_base("  "), "")

    def test_run_requirements_reject_missing_freemail_config(self):
        with self.assertRaises(app_config.ConfigError):
            app_config.validate_run_requirements(
                {**app_config.DEFAULT_CONFIG, "email_provider": "freemail"}
            )

    def test_run_requirements_accepts_bare_domain(self):
        cfg = app_config.validate_run_requirements(
            {**app_config.DEFAULT_CONFIG, **FREEMAIL_CONFIG}
        )
        self.assertEqual(cfg["email_provider"], "freemail")

    def test_run_requirements_accepts_full_url(self):
        cfg = app_config.validate_run_requirements(
            {
                **app_config.DEFAULT_CONFIG,
                "email_provider": "freemail",
                "freemail_domain": "https://mail.example.test",
                "freemail_token": "token-123",
            }
        )
        self.assertEqual(cfg["email_provider"], "freemail")

    def test_run_requirements_rejects_bad_domain(self):
        with self.assertRaises(app_config.ConfigError):
            app_config.validate_run_requirements(
                {
                    **app_config.DEFAULT_CONFIG,
                    "email_provider": "freemail",
                    "freemail_domain": "ftp://mail.example.test",
                    "freemail_token": "token-123",
                }
            )
        with self.assertRaises(app_config.ConfigError):
            app_config.validate_run_requirements(
                {
                    **app_config.DEFAULT_CONFIG,
                    "email_provider": "freemail",
                    "freemail_domain": "mail.example.test/path",
                    "freemail_token": "token-123",
                }
            )


class FreemailApiTests(unittest.TestCase):
    def setUp(self):
        self.prev_config = mail_service.config
        mail_service.config = dict(FREEMAIL_CONFIG)

    def tearDown(self):
        mail_service.config = self.prev_config

    def test_generate_returns_email_as_token(self):
        response = DummyResponse({"email": "abc123@example.test"})
        request = Mock(return_value=response)
        with patch.object(mail_service, "http_get", request, create=True):
            address, token = mail_freemail.freemail_get_email_and_token()
        self.assertEqual(address, "abc123@example.test")
        self.assertEqual(token, "abc123@example.test")
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://mail.example.test/api/generate")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-123")

    def test_generate_requires_email_field(self):
        response = DummyResponse({"unexpected": 1})
        with patch.object(mail_service, "http_get", return_value=response, create=True):
            with self.assertRaises(Exception):
                mail_freemail.freemail_get_email_and_token()

    def test_get_messages_passes_mailbox_param(self):
        response = DummyResponse(
            [{"id": 7, "subject": "Your xAI code", "verification_code": "ABC-123"}]
        )
        request = Mock(return_value=response)
        with patch.object(mail_service, "http_get", request, create=True):
            messages = mail_freemail.freemail_get_messages("abc123@example.test")
        self.assertEqual(len(messages), 1)
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://mail.example.test/api/emails")
        self.assertEqual(kwargs["params"]["mailbox"], "abc123@example.test")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-123")

    def test_get_messages_supports_wrapped_list_payload(self):
        response = DummyResponse({"data": [{"id": 1}]})
        with patch.object(mail_service, "http_get", return_value=response, create=True):
            self.assertEqual(len(mail_freemail.freemail_get_messages("a@b.c")), 1)

    def test_extract_body_from_eml_decodes_base64_html(self):
        raw = _eml_with_body("<body><p>Your verification code is XYZ-987.</p></body>")
        content = mail_freemail.freemail_extract_body_from_eml(raw)
        self.assertIn("XYZ-987", content)

    def test_extract_body_from_eml_falls_back_to_plain_text(self):
        raw = "Subject: hi\n\nYour verification code is XYZ-987."
        content = mail_freemail.freemail_extract_body_from_eml(raw)
        self.assertIn("XYZ-987", content)


class FreemailCodePollingTests(unittest.TestCase):
    def setUp(self):
        self.prev_config = mail_service.config
        mail_service.config = dict(FREEMAIL_CONFIG)

    def tearDown(self):
        mail_service.config = self.prev_config

    def _poll(self, messages, content=None):
        logs = []
        with patch.object(mail_service, "raise_if_cancelled", lambda cb=None: None, create=True), patch.object(
            mail_service, "sleep_with_cancel", lambda s, cb=None: None, create=True
        ), patch.object(mail_freemail, "freemail_get_messages", return_value=messages
        ), patch.object(mail_freemail, "freemail_get_message_content",
            return_value=content if content is not None else "",
        ):
            code = mail_freemail.freemail_get_oai_code(
                "ignored",
                "abc123@example.test",
                timeout=1,
                poll_interval=0,
                log_callback=logs.append,
            )
        return code, logs

    def test_verification_code_field_is_used_as_fallback(self):
        code, _ = self._poll([{"id": 7, "verification_code": "ABC-123"}])
        self.assertEqual(code, "ABC-123")

    def test_real_subject_wins_over_server_code_field(self):
        code, _ = self._poll(
            [
                {
                    "id": 7,
                    "subject": "Confirmation code: DEF-456",
                    "verification_code": "PER-110",
                }
            ]
        )
        self.assertEqual(code, "DEF-456")

    def test_downloaded_eml_body_is_extracted(self):
        code, logs = self._poll(
            [{"id": 8, "subject": "xAI"}],
            content="Your verification code is XYZ-987. Thanks.",
        )
        self.assertEqual(code, "XYZ-987")
        self.assertTrue(any("XYZ-987" in line for line in logs))

    def test_timeout_raises_verification_code_unavailable(self):
        with patch.object(mail_service, "raise_if_cancelled", lambda cb=None: None, create=True), patch.object(
            mail_service, "sleep_with_cancel", lambda s, cb=None: None, create=True
        ), patch.object(mail_freemail, "freemail_get_messages", return_value=[]):
            with self.assertRaises(mail_service.VerificationCodeUnavailable):
                mail_freemail.freemail_get_oai_code(
                    "ignored", "abc123@example.test", timeout=0.05, poll_interval=0
                )


class FreemailDispatchTests(unittest.TestCase):
    def setUp(self):
        self.prev_config = mail_service.config
        mail_service.config = dict(FREEMAIL_CONFIG)

    def tearDown(self):
        mail_service.config = self.prev_config

    def test_get_email_and_token_dispatches_to_freemail(self):
        with patch.object(mail_freemail, "freemail_get_email_and_token", return_value=("a@b.c", "a@b.c")) as mocked:
            self.assertEqual(
                mail_service.get_email_and_token(), ("a@b.c", "a@b.c")
            )
        mocked.assert_called_once_with()

    def test_get_oai_code_dispatches_to_freemail(self):
        with patch.object(mail_freemail, "freemail_get_oai_code", return_value="ABC-123") as mocked:
            self.assertEqual(
                mail_service.get_oai_code("a@b.c", "a@b.c", timeout=1),
                "ABC-123",
            )
        mocked.assert_called_once()
        self.assertIsNone(mocked.call_args.kwargs.get("resend_callback"))


if __name__ == "__main__":
    unittest.main()
