from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .constants import COMMON_HEADERS, NAVIGATE_HEADERS
from .email_providers import (
    extract_verification_code,
    fetch_email_detail_duckmail,
    fetch_emails,
    fetch_emails_duckmail,
    wait_for_verification_code_gateway,
    wait_for_verification_code_icloud,
)
from .sentinel import build_sentinel_token
from .utils import (
    create_session,
    generate_datadog_trace,
    get_ssl_verify,
    generate_pkce,
    generate_random_birthday,
    generate_random_name,
)


logger = logging.getLogger("pool_maintainer")

def codex_exchange_code(
    code: str,
    code_verifier: str,
    oauth_issuer: str,
    oauth_client_id: str,
    oauth_redirect_uri: str,
    proxy: str,
) -> Optional[Dict[str, Any]]:
    session = create_session(proxy=proxy)
    try:
        resp = session.post(
            f"{oauth_issuer}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": oauth_redirect_uri,
                "client_id": oauth_client_id,
                "code_verifier": code_verifier,
            },
            verify=get_ssl_verify(),
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else None
        logger.warning("OAuth 交换 token 失败: status=%s", resp.status_code)
        return None
    except Exception as e:
        logger.warning("OAuth 交换 token 异常: %s", e)
        return None


def perform_codex_oauth_login_http(
    email: str,
    password: str,
    cf_token: str,
    worker_domain: str,
    oauth_issuer: str,
    oauth_client_id: str,
    oauth_redirect_uri: str,
    proxy: str,
    email_provider: str = "cloudflare",
    duckmail_api_base: str = "",
    mail_gateway_base_url: str = "",
    mail_gateway_token: str = "",
    icloud_imap_host: str = "imap.mail.me.com",
    icloud_imap_port: int = 993,
    icloud_username: str = "",
    icloud_app_password: str = "",
) -> Optional[Dict[str, Any]]:
    session = create_session(proxy=proxy)
    device_id = str(uuid.uuid4())

    session.cookies.set("oai-did", device_id, domain=".auth.openai.com")
    session.cookies.set("oai-did", device_id, domain="auth.openai.com")

    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_urlsafe(32)

    authorize_params = {
        "response_type": "code",
        "client_id": oauth_client_id,
        "redirect_uri": oauth_redirect_uri,
        "scope": "openid profile email offline_access",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    authorize_url = f"{oauth_issuer}/oauth/authorize?{urlencode(authorize_params)}"

    try:
        session.get(
            authorize_url,
            headers=NAVIGATE_HEADERS,
            allow_redirects=True,
            verify=get_ssl_verify(),
            timeout=30,
        )
    except Exception as e:
        logger.warning("OAuth authorize 页面请求异常: email=%s err=%s", email, e)
        return None

    headers = dict(COMMON_HEADERS)
    headers["referer"] = f"{oauth_issuer}/log-in"
    headers["oai-device-id"] = device_id
    headers.update(generate_datadog_trace())

    sentinel_email = build_sentinel_token(session, device_id, flow="authorize_continue")
    if not sentinel_email:
        logger.warning("OAuth sentinel token 生成失败(authorize_continue): email=%s", email)
        return None
    headers["openai-sentinel-token"] = sentinel_email

    try:
        resp = session.post(
            f"{oauth_issuer}/api/accounts/authorize/continue",
            json={"username": {"kind": "email", "value": email}},
            headers=headers,
            verify=get_ssl_verify(),
            timeout=30,
        )
    except Exception as e:
        logger.warning("OAuth authorize/continue 异常: email=%s err=%s", email, e)
        return None

    if resp.status_code != 200:
        logger.warning("OAuth authorize/continue 失败: email=%s status=%s", email, resp.status_code)
        return None

    headers["referer"] = f"{oauth_issuer}/log-in/password"
    headers.update(generate_datadog_trace())

    sentinel_pwd = build_sentinel_token(session, device_id, flow="password_verify")
    if not sentinel_pwd:
        logger.warning("OAuth sentinel token 生成失败(password_verify): email=%s", email)
        return None
    headers["openai-sentinel-token"] = sentinel_pwd

    try:
        resp = session.post(
            f"{oauth_issuer}/api/accounts/password/verify",
            json={"password": password},
            headers=headers,
            verify=get_ssl_verify(),
            timeout=30,
            allow_redirects=False,
        )
    except Exception as e:
        logger.warning("OAuth password/verify 异常: email=%s err=%s", email, e)
        return None

    if resp.status_code != 200:
        logger.warning("OAuth password/verify 失败: email=%s status=%s", email, resp.status_code)
        return None

    continue_url = None
    page_type = ""
    try:
        data = resp.json()
        continue_url = str(data.get("continue_url") or "")
        page_type = str(((data.get("page") or {}).get("type")) or "")
    except Exception:
        pass

    if not continue_url:
        logger.warning("OAuth 缺少 continue_url: email=%s", email)
        return None

    if page_type == "email_otp_verification" or "email-verification" in continue_url:
        if email_provider not in ("icloud", "mail_gateway", "icloud_gateway") and not cf_token:
            logger.warning("OAuth 需要邮箱验证码但缺少 token: email=%s provider=%s", email, email_provider)
            return None

        mail_session = create_session(proxy=proxy)
        tried_codes = set()
        start_time = time.time()

        h_val = dict(COMMON_HEADERS)
        h_val["referer"] = f"{oauth_issuer}/email-verification"
        h_val["oai-device-id"] = device_id
        h_val.update(generate_datadog_trace())

        code = None

        if email_provider == "icloud":
            icloud_code = wait_for_verification_code_icloud(
                target_email=email,
                imap_host=icloud_imap_host,
                imap_port=icloud_imap_port,
                imap_user=icloud_username,
                imap_pass=icloud_app_password,
            )
            if icloud_code:
                resp_val = session.post(
                    f"{oauth_issuer}/api/accounts/email-otp/validate",
                    json={"code": icloud_code},
                    headers=h_val,
                    verify=get_ssl_verify(),
                    timeout=30,
                )
                if resp_val.status_code == 200:
                    code = icloud_code
                    try:
                        data = resp_val.json()
                        continue_url = str(data.get("continue_url") or "")
                        page_type = str(((data.get("page") or {}).get("type")) or "")
                    except Exception:
                        pass

        elif email_provider in ("mail_gateway", "icloud_gateway"):
            gateway_code = wait_for_verification_code_gateway(
                mail_session,
                gateway_base_url=mail_gateway_base_url,
                gateway_token=mail_gateway_token,
                mailbox_id=cf_token,
            )
            if gateway_code:
                resp_val = session.post(
                    f"{oauth_issuer}/api/accounts/email-otp/validate",
                    json={"code": gateway_code},
                    headers=h_val,
                    verify=get_ssl_verify(),
                    timeout=30,
                )
                if resp_val.status_code == 200:
                    code = gateway_code
                    try:
                        data = resp_val.json()
                        continue_url = str(data.get("continue_url") or "")
                        page_type = str(((data.get("page") or {}).get("type")) or "")
                    except Exception:
                        pass

        while not code and email_provider not in ("icloud", "mail_gateway", "icloud_gateway") and time.time() - start_time < 120:
            if email_provider == "duckmail":
                all_emails = fetch_emails_duckmail(mail_session, duckmail_api_base, cf_token)
            else:
                all_emails = fetch_emails(mail_session, worker_domain, cf_token)
            if not all_emails:
                time.sleep(2)
                continue

            all_codes = []
            for e_item in all_emails:
                if not isinstance(e_item, dict):
                    continue
                if email_provider == "duckmail":
                    msg_id = e_item.get("id") or e_item.get("@id")
                    if msg_id:
                        detail = fetch_email_detail_duckmail(mail_session, duckmail_api_base, cf_token, str(msg_id))
                        if detail:
                            content = detail.get("text") or detail.get("html") or ""
                            c = extract_verification_code(content)
                            if c and c not in tried_codes:
                                all_codes.append(c)
                else:
                    c = extract_verification_code(str(e_item.get("raw") or ""))
                    if c and c not in tried_codes:
                        all_codes.append(c)

            if not all_codes:
                time.sleep(2)
                continue

            for try_code in all_codes:
                tried_codes.add(try_code)
                resp_val = session.post(
                    f"{oauth_issuer}/api/accounts/email-otp/validate",
                    json={"code": try_code},
                    headers=h_val,
                    verify=get_ssl_verify(),
                    timeout=30,
                )
                if resp_val.status_code == 200:
                    code = try_code
                    try:
                        data = resp_val.json()
                        continue_url = str(data.get("continue_url") or "")
                        page_type = str(((data.get("page") or {}).get("type")) or "")
                    except Exception:
                        pass
                    break

            if code:
                break
            time.sleep(2)

        if not code:
            logger.warning("OAuth 邮箱验证码验证失败: email=%s provider=%s", email, email_provider)
            return None

        if "about-you" in continue_url:
            h_about = dict(NAVIGATE_HEADERS)
            h_about["referer"] = f"{oauth_issuer}/email-verification"
            try:
                resp_about = session.get(
                    f"{oauth_issuer}/about-you",
                    headers=h_about,
                    verify=get_ssl_verify(),
                    timeout=30,
                    allow_redirects=True,
                )
            except Exception:
                return None

            if "consent" in str(resp_about.url) or "organization" in str(resp_about.url):
                continue_url = str(resp_about.url)
            else:
                first_name, last_name = generate_random_name()
                birthdate = generate_random_birthday()

                h_create = dict(COMMON_HEADERS)
                h_create["referer"] = f"{oauth_issuer}/about-you"
                h_create["oai-device-id"] = device_id
                h_create.update(generate_datadog_trace())

                resp_create = session.post(
                    f"{oauth_issuer}/api/accounts/create_account",
                    json={"name": f"{first_name} {last_name}", "birthdate": birthdate},
                    headers=h_create,
                    verify=get_ssl_verify(),
                    timeout=30,
                )

                if resp_create.status_code == 200:
                    try:
                        data = resp_create.json()
                        continue_url = str(data.get("continue_url") or "")
                    except Exception:
                        pass
                elif resp_create.status_code == 400 and "already_exists" in resp_create.text:
                    continue_url = f"{oauth_issuer}/sign-in-with-chatgpt/codex/consent"

        if "consent" in page_type:
            continue_url = f"{oauth_issuer}/sign-in-with-chatgpt/codex/consent"

        if not continue_url or "email-verification" in continue_url:
            logger.warning("OAuth continue_url 无效: email=%s continue_url=%s", email, continue_url)
            return None

    if continue_url.startswith("/"):
        consent_url = f"{oauth_issuer}{continue_url}"
    else:
        consent_url = continue_url

    def _extract_code_from_url(url: str) -> Optional[str]:
        if not url or "code=" not in url:
            return None
        try:
            return parse_qs(urlparse(url).query).get("code", [None])[0]
        except Exception:
            return None

    def _decode_auth_session(session_obj: requests.Session) -> Optional[Dict[str, Any]]:
        for c in session_obj.cookies:
            if c.name == "oai-client-auth-session":
                val = c.value
                first_part = val.split(".")[0] if "." in val else val
                pad = 4 - len(first_part) % 4
                if pad != 4:
                    first_part += "=" * pad
                try:
                    raw = base64.urlsafe_b64decode(first_part)
                    d = json.loads(raw.decode("utf-8"))
                    return d if isinstance(d, dict) else None
                except Exception:
                    pass
        return None

    def _follow_and_extract_code(session_obj: requests.Session, url: str, max_depth: int = 10) -> Optional[str]:
        if max_depth <= 0:
            return None
        try:
            r = session_obj.get(
                url,
                headers=NAVIGATE_HEADERS,
                verify=get_ssl_verify(),
                timeout=15,
                allow_redirects=False,
            )
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                code = _extract_code_from_url(loc)
                if code:
                    return code
                if loc.startswith("/"):
                    loc = f"{oauth_issuer}{loc}"
                return _follow_and_extract_code(session_obj, loc, max_depth - 1)
            if r.status_code == 200:
                return _extract_code_from_url(str(r.url))
        except requests.exceptions.ConnectionError as e:
            m = re.search(r'(https?://localhost[^\s\'"]+)', str(e))
            if m:
                return _extract_code_from_url(m.group(1))
        except Exception:
            pass
        return None

    auth_code = None

    try:
        resp_consent = session.get(
            consent_url,
            headers=NAVIGATE_HEADERS,
            verify=get_ssl_verify(),
            timeout=30,
            allow_redirects=False,
        )
        if resp_consent.status_code in (301, 302, 303, 307, 308):
            loc = resp_consent.headers.get("Location", "")
            auth_code = _extract_code_from_url(loc)
            if not auth_code:
                auth_code = _follow_and_extract_code(session, loc)
    except requests.exceptions.ConnectionError as e:
        m = re.search(r'(https?://localhost[^\s\'"]+)', str(e))
        if m:
            auth_code = _extract_code_from_url(m.group(1))
    except Exception:
        pass

    if not auth_code:
        session_data = _decode_auth_session(session)
        workspace_id = None
        if session_data:
            workspaces = session_data.get("workspaces", [])
            if isinstance(workspaces, list) and workspaces:
                workspace_id = (workspaces[0] or {}).get("id")

        if workspace_id:
            h_consent = dict(COMMON_HEADERS)
            h_consent["referer"] = consent_url
            h_consent["oai-device-id"] = device_id
            h_consent.update(generate_datadog_trace())

            try:
                resp_ws = session.post(
                    f"{oauth_issuer}/api/accounts/workspace/select",
                    json={"workspace_id": workspace_id},
                    headers=h_consent,
                    verify=get_ssl_verify(),
                    timeout=30,
                    allow_redirects=False,
                )
                if resp_ws.status_code in (301, 302, 303, 307, 308):
                    loc = resp_ws.headers.get("Location", "")
                    auth_code = _extract_code_from_url(loc)
                    if not auth_code:
                        auth_code = _follow_and_extract_code(session, loc)
                elif resp_ws.status_code == 200:
                    ws_data = resp_ws.json()
                    ws_next = str(ws_data.get("continue_url") or "")
                    ws_page = str(((ws_data.get("page") or {}).get("type")) or "")

                    if "organization" in ws_next or "organization" in ws_page:
                        org_url = ws_next if ws_next.startswith("http") else f"{oauth_issuer}{ws_next}"

                        org_id = None
                        project_id = None
                        ws_orgs = (ws_data.get("data") or {}).get("orgs", []) if isinstance(ws_data, dict) else []
                        if ws_orgs:
                            org_id = (ws_orgs[0] or {}).get("id")
                            projects = (ws_orgs[0] or {}).get("projects", [])
                            if projects:
                                project_id = (projects[0] or {}).get("id")

                        if org_id:
                            body = {"org_id": org_id}
                            if project_id:
                                body["project_id"] = project_id

                            h_org = dict(COMMON_HEADERS)
                            h_org["referer"] = org_url
                            h_org["oai-device-id"] = device_id
                            h_org.update(generate_datadog_trace())

                            resp_org = session.post(
                                f"{oauth_issuer}/api/accounts/organization/select",
                                json=body,
                                headers=h_org,
                                verify=get_ssl_verify(),
                                timeout=30,
                                allow_redirects=False,
                            )
                            if resp_org.status_code in (301, 302, 303, 307, 308):
                                loc = resp_org.headers.get("Location", "")
                                auth_code = _extract_code_from_url(loc)
                                if not auth_code:
                                    auth_code = _follow_and_extract_code(session, loc)
                            elif resp_org.status_code == 200:
                                org_data = resp_org.json()
                                org_next = str(org_data.get("continue_url") or "")
                                if org_next:
                                    full_next = org_next if org_next.startswith("http") else f"{oauth_issuer}{org_next}"
                                    auth_code = _follow_and_extract_code(session, full_next)
                        else:
                            auth_code = _follow_and_extract_code(session, org_url)
                    else:
                        if ws_next:
                            full_next = ws_next if ws_next.startswith("http") else f"{oauth_issuer}{ws_next}"
                            auth_code = _follow_and_extract_code(session, full_next)
            except Exception:
                pass

    if not auth_code:
        try:
            resp_fallback = session.get(
                consent_url,
                headers=NAVIGATE_HEADERS,
                verify=get_ssl_verify(),
                timeout=30,
                allow_redirects=True,
            )
            auth_code = _extract_code_from_url(str(resp_fallback.url))
            if not auth_code and resp_fallback.history:
                for hist in resp_fallback.history:
                    loc = hist.headers.get("Location", "")
                    auth_code = _extract_code_from_url(loc)
                    if auth_code:
                        break
        except requests.exceptions.ConnectionError as e:
            m = re.search(r'(https?://localhost[^\s\'"]+)', str(e))
            if m:
                auth_code = _extract_code_from_url(m.group(1))
        except Exception:
            pass

    if not auth_code:
        logger.warning("OAuth 未获取到 authorization code: email=%s", email)
        return None

    return codex_exchange_code(
        auth_code,
        code_verifier,
        oauth_issuer=oauth_issuer,
        oauth_client_id=oauth_client_id,
        oauth_redirect_uri=oauth_redirect_uri,
        proxy=proxy,
    )


