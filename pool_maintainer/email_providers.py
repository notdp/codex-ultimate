from __future__ import annotations

import email as email_lib
import imaplib
import logging
import random
import re
import string
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from .utils import get_ssl_verify


def _bearer_header(token: str) -> Dict[str, str]:
    normalized = token.replace("Bearer ", "") if token.startswith("Bearer ") else token
    return {"Authorization": f"Bearer {normalized}"}


def _should_bypass_proxy_for_gateway(gateway_base_url: str) -> bool:
    host = (urlparse(gateway_base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}

def create_temp_email(
    session: requests.Session,
    worker_domain: str,
    email_domains: List[str],
    admin_password: str,
    logger: logging.Logger,
) -> tuple[Optional[str], Optional[str]]:
    name_len = random.randint(10, 14)
    name_chars = list(random.choices(string.ascii_lowercase, k=name_len))
    for _ in range(random.choice([1, 2])):
        pos = random.randint(2, len(name_chars) - 1)
        name_chars.insert(pos, random.choice(string.digits))
    name = "".join(name_chars)

    chosen_domain = random.choice(email_domains) if email_domains else "tuxixilax.cfd"

    try:
        res = session.post(
            f"https://{worker_domain}/admin/new_address",
            json={"enablePrefix": True, "name": name, "domain": chosen_domain},
            headers={"x-admin-auth": admin_password, "Content-Type": "application/json"},
            timeout=10,
            verify=get_ssl_verify(),
        )
        if res.status_code == 200:
            data = res.json()
            email = data.get("address")
            token = data.get("jwt")
            if email:
                logger.info("创建临时邮箱成功: %s (domain=%s)", email, chosen_domain)
                return str(email), str(token or "")
        logger.warning("创建临时邮箱失败: HTTP %s", res.status_code)
    except Exception as e:
        logger.warning("创建临时邮箱异常: %s", e)
    return None, None


def fetch_emails(session: requests.Session, worker_domain: str, cf_token: str) -> List[Dict[str, Any]]:
    try:
        res = session.get(
            f"https://{worker_domain}/api/mails",
            params={"limit": 10, "offset": 0},
            headers={"Authorization": f"Bearer {cf_token}"},
            verify=get_ssl_verify(),
            timeout=30,
        )
        if res.status_code == 200:
            rows = res.json().get("results", [])
            return rows if isinstance(rows, list) else []
    except Exception:
        pass
    return []


def extract_verification_code(content: str) -> Optional[str]:
    if not content:
        return None
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content)
    if m:
        return m.group(1)
    m = re.search(r"Subject:.*?(\d{6})", content)
    if m and m.group(1) != "177010":
        return m.group(1)
    for pat in [r">\s*(\d{6})\s*<", r"(?<![#&])\b(\d{6})\b"]:
        for code in re.findall(pat, content):
            if code != "177010":
                return code
    return None


def wait_for_verification_code(
    session: requests.Session,
    worker_domain: str,
    cf_token: str,
    timeout: int = 120,
) -> Optional[str]:
    old_ids = set()
    old = fetch_emails(session, worker_domain, cf_token)
    if old:
        old_ids = {e.get("id") for e in old if isinstance(e, dict) and "id" in e}
        for item in old:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("raw") or "")
            code = extract_verification_code(raw)
            if code:
                return code

    start = time.time()
    while time.time() - start < timeout:
        emails = fetch_emails(session, worker_domain, cf_token)
        if emails:
            for item in emails:
                if not isinstance(item, dict):
                    continue
                if item.get("id") in old_ids:
                    continue
                raw = str(item.get("raw") or "")
                code = extract_verification_code(raw)
                if code:
                    return code
        time.sleep(3)
    return None


def create_temp_email_duckmail(
    session: requests.Session,
    duckmail_api_base: str,
    duckmail_bearer: str,
    logger: logging.Logger,
) -> tuple[Optional[str], Optional[str]]:
    chars = string.ascii_lowercase + string.digits
    length = random.randint(8, 13)
    email_local = "".join(random.choice(chars) for _ in range(length))
    email = f"{email_local}@duckmail.sbs"
    password = "".join(random.choices(string.ascii_letters + string.digits + "!@#$%", k=14))

    api_base = duckmail_api_base.rstrip("/")
    bearer = duckmail_bearer.replace("Bearer ", "") if duckmail_bearer.startswith("Bearer ") else duckmail_bearer
    headers = {"Authorization": f"Bearer {bearer}"}

    try:
        res = session.post(
            f"{api_base}/accounts",
            json={"address": email, "password": password},
            headers=headers,
            timeout=30,
            verify=get_ssl_verify(),
        )
        if res.status_code not in [200, 201]:
            logger.warning("DuckMail 创建邮箱失败: HTTP %s", res.status_code)
            return None, None

        time.sleep(0.5)
        token_res = session.post(
            f"{api_base}/token",
            json={"address": email, "password": password},
            timeout=30,
            verify=get_ssl_verify(),
        )
        if token_res.status_code == 200:
            token_data = token_res.json()
            mail_token = token_data.get("token")
            if mail_token:
                logger.info("DuckMail 创建临时邮箱成功: %s", email)
                return email, mail_token
        logger.warning("DuckMail 获取邮件Token失败: HTTP %s", token_res.status_code)
    except Exception as e:
        logger.warning("DuckMail 创建临时邮箱异常: %s", e)
    return None, None


def fetch_emails_duckmail(session: requests.Session, duckmail_api_base: str, mail_token: str) -> List[Dict[str, Any]]:
    api_base = duckmail_api_base.rstrip("/")
    headers = {"Authorization": f"Bearer {mail_token}"}
    try:
        res = session.get(
            f"{api_base}/messages",
            headers=headers,
            timeout=30,
            verify=get_ssl_verify(),
        )
        if res.status_code == 200:
            data = res.json()
            messages = data.get("hydra:member") or data.get("member") or data.get("data") or []
            return messages if isinstance(messages, list) else []
    except Exception:
        pass
    return []


def fetch_email_detail_duckmail(session: requests.Session, duckmail_api_base: str, mail_token: str, msg_id: str) -> Optional[Dict[str, Any]]:
    api_base = duckmail_api_base.rstrip("/")
    headers = {"Authorization": f"Bearer {mail_token}"}
    if isinstance(msg_id, str) and msg_id.startswith("/messages/"):
        msg_id = msg_id.split("/")[-1]
    try:
        res = session.get(
            f"{api_base}/messages/{msg_id}",
            headers=headers,
            timeout=30,
            verify=get_ssl_verify(),
        )
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None


def wait_for_verification_code_duckmail(
    session: requests.Session,
    duckmail_api_base: str,
    mail_token: str,
    timeout: int = 120,
) -> Optional[str]:
    start = time.time()
    while time.time() - start < timeout:
        messages = fetch_emails_duckmail(session, duckmail_api_base, mail_token)
        if messages:
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id") or msg.get("@id")
                if not msg_id:
                    continue
                detail = fetch_email_detail_duckmail(session, duckmail_api_base, mail_token, str(msg_id))
                if detail:
                    content = detail.get("text") or detail.get("html") or ""
                    code = extract_verification_code(content)
                    if code:
                        return code
        time.sleep(3)
    return None


def create_temp_email_gateway(
    session: requests.Session,
    gateway_base_url: str,
    gateway_token: str,
    logger: logging.Logger,
) -> tuple[Optional[str], Optional[str]]:
    api_base = gateway_base_url.rstrip("/")
    headers = {**_bearer_header(gateway_token), "Content-Type": "application/json"}
    client = requests.Session() if _should_bypass_proxy_for_gateway(api_base) else session
    try:
        res = client.post(
            f"{api_base}/mailboxes",
            json={},
            headers=headers,
            timeout=30,
            verify=get_ssl_verify(),
        )
        if res.status_code == 200:
            data = res.json()
            address = data.get("address")
            mailbox_id = data.get("mailbox_id")
            if address and mailbox_id:
                logger.info("Mail Gateway 创建临时邮箱成功: %s", address)
                return str(address), str(mailbox_id)
        logger.warning("Mail Gateway 创建邮箱失败: HTTP %s", res.status_code)
    except Exception as e:
        logger.warning("Mail Gateway 创建邮箱异常: %s", e)
    return None, None


def wait_for_verification_code_gateway(
    session: requests.Session,
    gateway_base_url: str,
    gateway_token: str,
    mailbox_id: str,
    timeout: int = 120,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    api_base = gateway_base_url.rstrip("/")
    headers = _bearer_header(gateway_token)
    client = requests.Session() if _should_bypass_proxy_for_gateway(api_base) else session
    try:
        res = client.get(
            f"{api_base}/mailboxes/{mailbox_id}/otp",
            params={"timeout": timeout},
            headers=headers,
            timeout=timeout + 10,
            verify=get_ssl_verify(),
        )
        if res.status_code == 200:
            data = res.json()
            code = data.get("code")
            if code:
                if logger:
                    logger.info("Mail Gateway 收到验证码: %s (mailbox=%s)", code, mailbox_id)
                return str(code)
        if logger:
            logger.warning("Mail Gateway 拉取验证码失败: HTTP %s", res.status_code)
    except Exception as e:
        if logger:
            logger.warning("Mail Gateway 拉取验证码异常: %s", e)
    return None


def create_temp_email_icloud(
    email_domains: List[str],
    logger: logging.Logger,
) -> tuple[Optional[str], Optional[str]]:
    chars = string.ascii_lowercase + string.digits
    length = random.randint(10, 14)
    local_part = "".join(random.choice(chars) for _ in range(length))
    domain = random.choice(email_domains) if email_domains else "franxx.ai"
    email_addr = f"{local_part}@{domain}"
    logger.info("iCloud Catch-All 生成邮箱: %s", email_addr)
    return email_addr, "icloud"


def wait_for_verification_code_icloud(
    target_email: str,
    imap_host: str,
    imap_port: int,
    imap_user: str,
    imap_pass: str,
    timeout: int = 120,
    logger: Optional[logging.Logger] = None,
) -> Optional[str]:
    start = time.time()
    seen_uids: set[str] = set()
    while time.time() - start < timeout:
        conn = None
        try:
            conn = imaplib.IMAP4_SSL(imap_host, imap_port)
            conn.login(imap_user, imap_pass)
            conn.select("INBOX")

            search_criteria = f'(TO "{target_email}" FROM "openai.com")'
            status, msg_ids = conn.search(None, search_criteria)
            if status != "OK" or not msg_ids or not msg_ids[0]:
                status, msg_ids = conn.search(None, f'(TO "{target_email}")')

            if status == "OK" and msg_ids and msg_ids[0]:
                uid_list = msg_ids[0].split()
                for uid in reversed(uid_list):
                    uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                    if uid_str in seen_uids:
                        continue
                    seen_uids.add(uid_str)
                    _, msg_data = conn.fetch(uid, "(BODY[])")
                    if not msg_data or not msg_data[0]:
                        continue
                    raw_email = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                    if not raw_email:
                        continue
                    msg = email_lib.message_from_bytes(raw_email)
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct in ("text/html", "text/plain"):
                                payload = part.get_payload(decode=True)
                                if payload:
                                    charset = part.get_content_charset() or "utf-8"
                                    body += payload.decode(charset, errors="replace")
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            charset = msg.get_content_charset() or "utf-8"
                            body = payload.decode(charset, errors="replace")
                    if body:
                        code = extract_verification_code(body)
                        if code:
                            if logger:
                                logger.info("iCloud IMAP 收到验证码: %s (email=%s)", code, target_email)
                            return code
            conn.logout()
        except Exception as e:
            if logger:
                logger.warning("iCloud IMAP 轮询异常: %s", e)
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass
        time.sleep(3)
    return None
