"""Freemail 临时邮箱提供商实现（fork 本地扩展，独立模块以降低上游合并冲突面）。

接口契约（与 grok-register-back 保持一致）：
- GET /api/generate            -> {"email": "..."}，无独立凭证，邮箱地址即收件箱标识
- GET /api/emails?mailbox=...  -> 邮件列表，条目可能带 verification_code 字段
- GET /api/email/{id}/download -> 原始 EML 文本

本模块只被 mail_service.get_email_and_token / get_oai_code 的 dispatch 分支
惰性导入。运行期依赖（config / http_get / sleep_with_cancel /
raise_if_cancelled 及邮件解析辅助函数）在调用时从 mail_service 动态获取，
mail_service.bind_runtime 注入完成后即可正常工作。
"""

import base64
import re
import time

import mail_service as _ms


def freemail_normalize_api_base(value):
    """兼容裸域名（mail.example.com）与完整 http(s) URL 两种写法。"""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" in raw:
        return raw
    return f"https://{raw}"


def get_freemail_api_base():
    return freemail_normalize_api_base(_ms.config.get("freemail_domain", ""))


def get_freemail_token():
    return str(_ms.config.get("freemail_token", "") or "").strip()


def freemail_build_headers():
    token = get_freemail_token()
    if not token:
        raise Exception("Freemail Token 未配置")
    return {"Authorization": f"Bearer {token}"}


def freemail_strip_html(html):
    """极简 HTML 转文本，用于解析 freemail 邮件正文。"""
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    if body_match:
        html = body_match.group(1)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def freemail_extract_body_from_eml(raw_email):
    """从 /api/email/{id}/download 返回的原始 EML 文本中提取可读正文。"""
    text = str(raw_email or "")
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n")
    match = re.search(r"\n\n([A-Za-z0-9+/=\n]{20,})", normalized)
    if match:
        try:
            decoded = base64.b64decode(match.group(1).replace("\n", "")).decode(
                "utf-8", errors="ignore"
            )
            stripped = freemail_strip_html(decoded)
            if stripped:
                return stripped
        except Exception:
            pass
    return freemail_strip_html(normalized)


def freemail_get_email_and_token():
    """调用 freemail /api/generate 创建临时邮箱。

    freemail 直接用邮箱地址查询收件箱，不返回独立凭证，因此 dev_token 即邮箱本身。
    """
    api_base = get_freemail_api_base()
    if not api_base:
        raise Exception("Freemail 域名未配置")
    resp = _ms.http_get(f"{api_base}/api/generate", headers=freemail_build_headers())
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Freemail /api/generate 返回非JSON: {resp.text[:300]}")
    address = str((data or {}).get("email", "") or "").strip()
    if not address:
        raise Exception(f"Freemail /api/generate 响应缺少 email 字段: {data}")
    return address, address


def freemail_get_messages(mailbox):
    api_base = get_freemail_api_base()
    if not api_base:
        raise Exception("Freemail 域名未配置")
    resp = _ms.http_get(
        f"{api_base}/api/emails",
        params={"mailbox": mailbox},
        headers=freemail_build_headers(),
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Freemail /api/emails 返回非JSON: {resp.text[:300]}")
    if isinstance(data, list):
        return data
    return _ms._pick_list_payload(data)


def freemail_get_message_content(message_id):
    api_base = get_freemail_api_base()
    if not api_base:
        raise Exception("Freemail 域名未配置")
    resp = _ms.http_get(
        f"{api_base}/api/email/{message_id}/download",
        headers=freemail_build_headers(),
    )
    resp.raise_for_status()
    return freemail_extract_body_from_eml(getattr(resp, "text", "") or "")


def freemail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    mailbox = str(email or dev_token or "").strip()
    deadline = time.time() + timeout
    detail_retries = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        _ms.raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = freemail_get_messages(mailbox)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Freemail 拉取邮件列表失败: {exc}")
            _ms.sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Freemail 本轮邮件数量: {len(messages)}")
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if msg_id is None:
                continue
            subject = str(msg.get("subject", "") or "")
            combined = _ms.normalize_mail_body(msg)
            # 优先真实邮件内容；freemail 服务端的 verification_code 字段作为回退。
            code = _ms.extract_verification_code(combined, subject)
            code_field = str(msg.get("verification_code", "") or "").strip()
            if not code and code_field:
                code = _ms.extract_verification_code(f"verification code: {code_field}")
            if code:
                if log_callback:
                    log_callback(f"[*] Freemail 从邮件中提取到验证码: {code}")
                return code

            detail_attempt = _ms._detail_retry_attempt(detail_retries, msg_id)
            if not detail_attempt:
                continue
            try:
                content = freemail_get_message_content(msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(
                        f"[Debug] Freemail 下载邮件正文失败 id={msg_id} "
                        f"attempt={detail_attempt}: {exc}"
                    )
                continue
            if log_callback:
                log_callback(f"[Debug] Freemail 收到邮件: {subject}")
            code = _ms.extract_verification_code(content, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Freemail 从邮件正文中提取到验证码: {code}")
                return code
        _ms.sleep_with_cancel(poll_interval, cancel_callback)
    raise _ms.VerificationCodeUnavailable(
        f"Freemail 在 {timeout}s 内未收到验证码邮件"
    )
