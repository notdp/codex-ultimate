from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

from .constants import COMMON_HEADERS, NAVIGATE_HEADERS, OPENAI_AUTH_BASE
from .email_providers import (
    wait_for_verification_code,
    wait_for_verification_code_duckmail,
    wait_for_verification_code_gateway,
    wait_for_verification_code_icloud,
)
from .sentinel import SentinelTokenGenerator, build_sentinel_token
from .utils import (
    create_session,
    generate_datadog_trace,
    get_ssl_verify,
    generate_pkce,
    generate_random_birthday,
    generate_random_name,
)

class ProtocolRegistrar:
    def __init__(self, proxy: str, logger: logging.Logger):
        self.session = create_session(proxy=proxy)
        self.device_id = str(uuid.uuid4())
        self.logger = logger
        self.sentinel_gen = SentinelTokenGenerator(device_id=self.device_id)
        self.code_verifier: Optional[str] = None
        self.state: Optional[str] = None

    def _build_headers(self, referer: str, with_sentinel: bool = False) -> Dict[str, str]:
        h = dict(COMMON_HEADERS)
        h["referer"] = referer
        h["oai-device-id"] = self.device_id
        h.update(generate_datadog_trace())
        if with_sentinel:
            h["openai-sentinel-token"] = self.sentinel_gen.generate_token()
        return h

    def step0_init_oauth_session(self, email: str, client_id: str, redirect_uri: str) -> bool:
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

        code_verifier, code_challenge = generate_pkce()
        self.code_verifier = code_verifier
        self.state = secrets.token_urlsafe(32)

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            "screen_hint": "signup",
            "prompt": "login",
        }

        url = f"{OPENAI_AUTH_BASE}/oauth/authorize?{urlencode(params)}"
        try:
            resp = self.session.get(url, headers=NAVIGATE_HEADERS, allow_redirects=True, verify=get_ssl_verify(), timeout=30)
        except Exception as e:
            self.logger.warning("步骤0a失败: %s", e)
            return False
        if resp.status_code not in (200, 302):
            self.logger.warning(
                "步骤0a失败: OAuth初始化状态码异常 status=%s, url=%s, 响应预览=%s",
                resp.status_code,
                str(resp.url),
                (resp.text or "")[:300].replace("\n", " "),
            )
            return False

        has_login_session = any(c.name == "login_session" for c in self.session.cookies)
        if not has_login_session:
            cookie_names = [c.name for c in self.session.cookies]
            self.logger.warning(
                "步骤0a失败: 未获取 login_session cookie, cookies=%s, status=%s, url=%s, 响应预览=%s",
                cookie_names,
                resp.status_code,
                str(resp.url),
                (resp.text or "")[:300].replace("\n", " "),
            )
            return False

        headers = self._build_headers(f"{OPENAI_AUTH_BASE}/create-account")
        sentinel = build_sentinel_token(self.session, self.device_id, flow="authorize_continue")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        try:
            r2 = self.session.post(
                f"{OPENAI_AUTH_BASE}/api/accounts/authorize/continue",
                json={"username": {"kind": "email", "value": email}, "screen_hint": "signup"},
                headers=headers,
                verify=get_ssl_verify(),
                timeout=30,
            )
            if r2.status_code != 200:
                self.logger.warning(
                    "步骤0b失败: authorize/continue 返回异常 status=%s, email=%s, 响应预览=%s",
                    r2.status_code,
                    email,
                    (r2.text or "")[:300].replace("\n", " "),
                )
            return r2.status_code == 200
        except Exception as e:
            self.logger.warning("步骤0b异常: %s | email=%s", e, email)
            return False

    def step2_register_user(self, email: str, password: str) -> bool:
        headers = self._build_headers(
            f"{OPENAI_AUTH_BASE}/create-account/password",
            with_sentinel=True,
        )
        try:
            resp = self.session.post(
                f"{OPENAI_AUTH_BASE}/api/accounts/user/register",
                json={"username": email, "password": password},
                headers=headers,
                verify=get_ssl_verify(),
                timeout=30,
            )
            if resp.status_code == 200:
                return True
            if resp.status_code in (301, 302):
                loc = resp.headers.get("Location", "")
                ok_redirect = "email-otp" in loc or "email-verification" in loc
                if not ok_redirect:
                    self.logger.warning(
                        "步骤2失败: register重定向异常 status=%s, location=%s, email=%s",
                        resp.status_code,
                        loc,
                        email,
                    )
                return ok_redirect
            self.logger.warning(
                "步骤2失败: register返回异常 status=%s, email=%s, 响应预览=%s",
                resp.status_code,
                email,
                (resp.text or "")[:300].replace("\n", " "),
            )
            return False
        except Exception as e:
            self.logger.warning("步骤2异常: %s | email=%s", e, email)
            return False

    def step3_send_otp(self) -> bool:
        try:
            h = dict(NAVIGATE_HEADERS)
            h["referer"] = f"{OPENAI_AUTH_BASE}/create-account/password"
            r_send = self.session.get(
                f"{OPENAI_AUTH_BASE}/api/accounts/email-otp/send",
                headers=h,
                verify=get_ssl_verify(),
                timeout=30,
                allow_redirects=True,
            )
            r_page = self.session.get(
                f"{OPENAI_AUTH_BASE}/email-verification",
                headers=h,
                verify=get_ssl_verify(),
                timeout=30,
                allow_redirects=True,
            )
            if r_send.status_code >= 400 or r_page.status_code >= 400:
                self.logger.warning(
                    "步骤3告警: 发送OTP或进入验证页状态异常 send=%s page=%s",
                    r_send.status_code,
                    r_page.status_code,
                )
            return True
        except Exception as e:
            self.logger.warning("步骤3异常: %s", e)
            return False

    def step4_validate_otp(self, code: str) -> bool:
        h = self._build_headers(f"{OPENAI_AUTH_BASE}/email-verification")
        try:
            r = self.session.post(
                f"{OPENAI_AUTH_BASE}/api/accounts/email-otp/validate",
                json={"code": code},
                headers=h,
                verify=get_ssl_verify(),
                timeout=30,
            )
            if r.status_code != 200:
                self.logger.warning(
                    "步骤4失败: OTP验证失败 status=%s, code=%s, 响应预览=%s",
                    r.status_code,
                    code,
                    (r.text or "")[:300].replace("\n", " "),
                )
            return r.status_code == 200
        except Exception as e:
            self.logger.warning("步骤4异常: %s", e)
            return False

    def step5_create_account(self, first_name: str, last_name: str, birthdate: str) -> bool:
        h = self._build_headers(f"{OPENAI_AUTH_BASE}/about-you")
        body = {"name": f"{first_name} {last_name}", "birthdate": birthdate}
        try:
            r = self.session.post(
                f"{OPENAI_AUTH_BASE}/api/accounts/create_account",
                json=body,
                headers=h,
                verify=get_ssl_verify(),
                timeout=30,
            )
            if r.status_code == 200:
                return True
            if r.status_code == 403 and "sentinel" in r.text.lower():
                self.logger.warning("步骤5告警: create_account 命中sentinel风控，尝试重试")
                h["openai-sentinel-token"] = SentinelTokenGenerator(self.device_id).generate_token()
                rr = self.session.post(
                    f"{OPENAI_AUTH_BASE}/api/accounts/create_account",
                    json=body,
                    headers=h,
                    verify=get_ssl_verify(),
                    timeout=30,
                )
                if rr.status_code != 200:
                    self.logger.warning(
                        "步骤5失败: sentinel重试后仍失败 status=%s, 响应预览=%s",
                        rr.status_code,
                        (rr.text or "")[:300].replace("\n", " "),
                    )
                return rr.status_code == 200
            if r.status_code not in (301, 302):
                self.logger.warning(
                    "步骤5失败: create_account返回异常 status=%s, 响应预览=%s",
                    r.status_code,
                    (r.text or "")[:300].replace("\n", " "),
                )
            return r.status_code in (301, 302)
        except Exception as e:
            self.logger.warning("步骤5异常: %s", e)
            return False

    def register(
        self,
        email: str,
        worker_domain: str,
        cf_token: str,
        password: str,
        client_id: str,
        redirect_uri: str,
        email_provider: str = "cloudflare",
        duckmail_api_base: str = "",
        mail_gateway_base_url: str = "",
        mail_gateway_token: str = "",
        icloud_imap_host: str = "imap.mail.me.com",
        icloud_imap_port: int = 993,
        icloud_username: str = "",
        icloud_app_password: str = "",
    ) -> bool:
        first_name, last_name = generate_random_name()
        birthdate = generate_random_birthday()
        if not self.step0_init_oauth_session(email, client_id, redirect_uri):
            self.logger.warning("注册失败: step0_init_oauth_session | email=%s", email)
            return False
        time.sleep(1)
        if not self.step2_register_user(email, password):
            self.logger.warning("注册失败: step2_register_user | email=%s", email)
            return False
        time.sleep(1)
        if not self.step3_send_otp():
            self.logger.warning("注册失败: step3_send_otp | email=%s", email)
            return False
        mail_session = create_session()
        if email_provider == "icloud":
            code = wait_for_verification_code_icloud(
                target_email=email,
                imap_host=icloud_imap_host,
                imap_port=icloud_imap_port,
                imap_user=icloud_username,
                imap_pass=icloud_app_password,
                logger=self.logger,
            )
        elif email_provider in ("mail_gateway", "icloud_gateway"):
            code = wait_for_verification_code_gateway(
                mail_session,
                gateway_base_url=mail_gateway_base_url,
                gateway_token=mail_gateway_token,
                mailbox_id=cf_token,
                logger=self.logger,
            )
        elif email_provider == "duckmail":
            code = wait_for_verification_code_duckmail(mail_session, duckmail_api_base, cf_token)
        else:
            code = wait_for_verification_code(mail_session, worker_domain, cf_token)
        if not code:
            self.logger.warning("注册失败: 未收到验证码 | email=%s", email)
            return False
        if not self.step4_validate_otp(code):
            self.logger.warning("注册失败: step4_validate_otp | email=%s", email)
            return False
        time.sleep(1)
        ok = self.step5_create_account(first_name, last_name, birthdate)
        if not ok:
            self.logger.warning("注册失败: step5_create_account | email=%s", email)
        return ok


