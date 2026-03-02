from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional

from .config import ensure_parent_dir, pick_conf
from .email_providers import create_temp_email, create_temp_email_duckmail, create_temp_email_gateway, create_temp_email_icloud
from .oauth import perform_codex_oauth_login_http
from .registrar import ProtocolRegistrar
from .utils import create_session, decode_jwt_payload, generate_random_password, get_ssl_verify

class RegisterRuntime:
    def __init__(self, conf: Dict[str, Any], target_tokens: int, logger: logging.Logger):
        self.conf = conf
        self.target_tokens = target_tokens
        self.logger = logger

        self.file_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.token_success_count = 0
        self.stop_event = threading.Event()

        run_workers = int(pick_conf(conf, "run", "workers", default=1) or 1)
        self.concurrent_workers = max(1, run_workers)
        self.proxy = str(pick_conf(conf, "run", "proxy", default="") or "")

        self.worker_domain = str(pick_conf(conf, "email", "worker_domain", default="email.tuxixilax.cfd") or "")
        old_domain = str(pick_conf(conf, "email", "email_domain", default="tuxixilax.cfd") or "tuxixilax.cfd")
        domains = pick_conf(conf, "email", "email_domains", default=None)
        parsed_domains: List[str] = []
        if isinstance(domains, list):
            parsed_domains = [str(x).strip() for x in domains if str(x).strip()]
        if not parsed_domains:
            parsed_domains = [old_domain]
        self.email_domains = parsed_domains
        self.admin_password = str(pick_conf(conf, "email", "admin_password", default="") or "")

        self.email_provider = str(pick_conf(conf, "email", "provider", default="cloudflare") or "cloudflare").lower()
        self.duckmail_api_base = str(conf.get("duckmail_api_base", "https://api.duckmail.sbs") or "https://api.duckmail.sbs")
        self.duckmail_bearer = str(conf.get("duckmail_bearer", "") or "")

        mail_gateway_cfg = conf.get("mail_gateway") if isinstance(conf.get("mail_gateway"), dict) else {}
        self.mail_gateway_base_url = str(mail_gateway_cfg.get("base_url", "") or "")
        self.mail_gateway_token = str(mail_gateway_cfg.get("token", "") or "")

        icloud_cfg = conf.get("icloud") if isinstance(conf.get("icloud"), dict) else {}
        self.icloud_imap_host = str(icloud_cfg.get("imap_host", "imap.mail.me.com") or "imap.mail.me.com")
        self.icloud_imap_port = int(icloud_cfg.get("imap_port", 993) or 993)
        self.icloud_username = str(icloud_cfg.get("username", "") or "")
        self.icloud_app_password = str(icloud_cfg.get("app_password", "") or "")

        self.oauth_issuer = str(pick_conf(conf, "oauth", "issuer", default="https://auth.openai.com") or "https://auth.openai.com")
        self.oauth_client_id = str(
            pick_conf(conf, "oauth", "client_id", default="app_EMoamEEZ73f0CkXaXp7hrann") or "app_EMoamEEZ73f0CkXaXp7hrann"
        )
        self.oauth_redirect_uri = str(
            pick_conf(conf, "oauth", "redirect_uri", default="http://localhost:1455/auth/callback")
            or "http://localhost:1455/auth/callback"
        )
        self.oauth_retry_attempts = int(pick_conf(conf, "oauth", "retry_attempts", default=3) or 3)
        self.oauth_retry_backoff_base = float(pick_conf(conf, "oauth", "retry_backoff_base", default=2.0) or 2.0)
        self.oauth_retry_backoff_max = float(pick_conf(conf, "oauth", "retry_backoff_max", default=15.0) or 15.0)

        upload_base = str(pick_conf(conf, "upload", "cli_proxy_api_base", "base_url", default="") or "").strip()
        if not upload_base:
            upload_base = str(pick_conf(conf, "clean", "base_url", default="") or "").strip()
        self.cli_proxy_api_base = upload_base.rstrip("/")

        upload_token = str(pick_conf(conf, "upload", "token", "cpa_password", default="") or "").strip()
        if not upload_token:
            upload_token = str(pick_conf(conf, "clean", "token", "cpa_password", default="") or "").strip()
        self.upload_api_token = upload_token

        self.upload_url = f"{self.cli_proxy_api_base}/v0/management/auth-files" if self.cli_proxy_api_base else ""

        output_cfg = conf.get("output")
        if not isinstance(output_cfg, dict):
            output_cfg = {}

        save_local_raw = output_cfg.get("save_local", True)
        if isinstance(save_local_raw, bool):
            self.save_local = save_local_raw
        else:
            self.save_local = str(save_local_raw).strip().lower() in ("1", "true", "yes", "on")

        self.run_dir = os.getcwd()
        if self.save_local:
            self.fixed_out_dir = os.path.join(self.run_dir, "output_fixed")
            self.tokens_parent_dir = os.path.join(self.run_dir, "output_tokens")
            os.makedirs(self.fixed_out_dir, exist_ok=True)
            os.makedirs(self.tokens_parent_dir, exist_ok=True)
            self.tokens_out_dir = self._ensure_unique_dir(self.tokens_parent_dir, f"{target_tokens}个账号")

            self.accounts_file = self._resolve_output_path(str(output_cfg.get("accounts_file", "accounts.txt")))
            self.csv_file = self._resolve_output_path(str(output_cfg.get("csv_file", "registered_accounts.csv")))
            self.ak_file = self._resolve_output_path(str(output_cfg.get("ak_file", "ak.txt")))
            self.rk_file = self._resolve_output_path(str(output_cfg.get("rk_file", "rk.txt")))
        else:
            self.fixed_out_dir = ""
            self.tokens_parent_dir = ""
            self.tokens_out_dir = ""
            self.accounts_file = ""
            self.csv_file = ""
            self.ak_file = ""
            self.rk_file = ""

    def _resolve_output_path(self, value: str) -> str:
        if os.path.isabs(value):
            return value
        return os.path.join(self.fixed_out_dir, value)

    def _ensure_unique_dir(self, parent_dir: str, base_name: str) -> str:
        os.makedirs(parent_dir, exist_ok=True)

        candidates = [os.path.join(parent_dir, base_name)] + [
            os.path.join(parent_dir, f"{base_name}-{idx}") for idx in range(1, 1000000)
        ]
        for candidate in candidates:
            try:
                os.makedirs(candidate)
                return candidate
            except FileExistsError:
                continue
        raise RuntimeError(f"无法创建唯一目录: {parent_dir}/{base_name}")

    def get_token_success_count(self) -> int:
        with self.counter_lock:
            return self.token_success_count

    def claim_token_slot(self) -> tuple[bool, int]:
        with self.counter_lock:
            if self.token_success_count >= self.target_tokens:
                return False, self.token_success_count
            self.token_success_count += 1
            if self.token_success_count >= self.target_tokens:
                self.stop_event.set()
            return True, self.token_success_count

    def release_token_slot(self) -> None:
        with self.counter_lock:
            if self.token_success_count > 0:
                self.token_success_count -= 1
            if self.token_success_count < self.target_tokens:
                self.stop_event.clear()

    def save_token_json(self, email: str, access_token: str, refresh_token: str = "", id_token: str = "") -> bool:
        try:
            payload = decode_jwt_payload(access_token)
            auth_info = payload.get("https://api.openai.com/auth", {})
            account_id = auth_info.get("chatgpt_account_id", "") if isinstance(auth_info, dict) else ""

            exp_timestamp = payload.get("exp", 0)
            expired_str = ""
            if exp_timestamp:
                exp_dt = dt.datetime.fromtimestamp(exp_timestamp, tz=dt.timezone(dt.timedelta(hours=8)))
                expired_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

            now = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8)))
            token_data = {
                "type": "codex",
                "email": email,
                "expired": expired_str,
                "id_token": id_token or "",
                "account_id": account_id,
                "access_token": access_token,
                "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "refresh_token": refresh_token or "",
            }

            if self.save_local:
                filename = os.path.join(self.tokens_out_dir, f"{email}.json")
                ensure_parent_dir(filename)
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, ensure_ascii=False)

                if self.upload_url and self.upload_api_token:
                    self.upload_token_json(filename)
            else:
                if self.upload_url and self.upload_api_token:
                    self.upload_token_data(f"{email}.json", token_data)

            return True
        except Exception as e:
            self.logger.warning("保存 Token JSON 失败: %s", e)
            return False

    def upload_token_json(self, filename: str) -> None:
        if not self.upload_url or not self.upload_api_token:
            return
        try:
            s = create_session(proxy=self.proxy)
            with open(filename, "rb") as f:
                files = {"file": (os.path.basename(filename), f, "application/json")}
                headers = {"Authorization": f"Bearer {self.upload_api_token}"}
                resp = s.post(self.upload_url, files=files, headers=headers, verify=get_ssl_verify(), timeout=30)
                if resp.status_code != 200:
                    self.logger.warning("上传 token 失败: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            self.logger.warning("上传 token 异常: %s", e)

    def upload_token_data(self, filename: str, token_data: Dict[str, Any]) -> None:
        if not self.upload_url or not self.upload_api_token:
            return
        try:
            s = create_session(proxy=self.proxy)
            content = json.dumps(token_data, ensure_ascii=False).encode("utf-8")
            files = {"file": (filename, content, "application/json")}
            headers = {"Authorization": f"Bearer {self.upload_api_token}"}
            resp = s.post(self.upload_url, files=files, headers=headers, verify=get_ssl_verify(), timeout=30)
            if resp.status_code != 200:
                self.logger.warning("上传 token 失败: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            self.logger.warning("上传 token 异常: %s", e)

    def save_tokens(self, email: str, tokens: Dict[str, Any]) -> bool:
        access_token = str(tokens.get("access_token") or "")
        refresh_token = str(tokens.get("refresh_token") or "")
        id_token = str(tokens.get("id_token") or "")

        if self.save_local:
            try:
                with self.file_lock:
                    if access_token:
                        ensure_parent_dir(self.ak_file)
                        with open(self.ak_file, "a", encoding="utf-8") as f:
                            f.write(f"{access_token}\n")
                    if refresh_token:
                        ensure_parent_dir(self.rk_file)
                        with open(self.rk_file, "a", encoding="utf-8") as f:
                            f.write(f"{refresh_token}\n")
            except Exception as e:
                self.logger.warning("AK/RK 保存失败: %s", e)
                return False

        if access_token:
            return self.save_token_json(email, access_token, refresh_token, id_token)
        return False

    def save_account(self, email: str, password: str) -> None:
        if not self.save_local:
            return

        with self.file_lock:
            ensure_parent_dir(self.accounts_file)
            ensure_parent_dir(self.csv_file)

            with open(self.accounts_file, "a", encoding="utf-8") as f:
                f.write(f"{email}:{password}\n")

            file_exists = os.path.exists(self.csv_file)
            with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["email", "password", "timestamp"])
                writer.writerow([email, password, time.strftime("%Y-%m-%d %H:%M:%S")])

    def collect_token_emails(self) -> set[str]:
        emails = set()
        if not os.path.isdir(self.tokens_out_dir):
            return emails
        for name in os.listdir(self.tokens_out_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.tokens_out_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                email = data.get("email") or name[:-5]
                if email:
                    emails.add(str(email))
            except Exception:
                continue
        return emails

    def reconcile_account_outputs_from_tokens(self) -> int:
        if not self.save_local:
            return 0

        token_emails = self.collect_token_emails()

        pwd_map: Dict[str, str] = {}
        if os.path.exists(self.accounts_file):
            try:
                with open(self.accounts_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or ":" not in line:
                            continue
                        email, pwd = line.split(":", 1)
                        pwd_map[email] = pwd
            except Exception:
                pass

        ordered_emails = sorted(token_emails)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        with self.file_lock:
            ensure_parent_dir(self.accounts_file)
            ensure_parent_dir(self.csv_file)

            with open(self.accounts_file, "w", encoding="utf-8") as f:
                for email in ordered_emails:
                    f.write(f"{email}:{pwd_map.get(email, '')}\n")

            with open(self.csv_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["email", "password", "timestamp"])
                for email in ordered_emails:
                    writer.writerow([email, pwd_map.get(email, ""), timestamp])

        return len(ordered_emails)

    def oauth_login_with_retry(self, email: str, password: str, cf_token: str) -> Optional[Dict[str, Any]]:
        attempts = max(1, self.oauth_retry_attempts)
        for attempt in range(1, attempts + 1):
            if self.stop_event.is_set() and self.get_token_success_count() >= self.target_tokens:
                return None

            self.logger.info("OAuth 尝试 %s/%s: %s", attempt, attempts, email)
            tokens = perform_codex_oauth_login_http(
                email=email,
                password=password,
                cf_token=cf_token,
                worker_domain=self.worker_domain,
                oauth_issuer=self.oauth_issuer,
                oauth_client_id=self.oauth_client_id,
                oauth_redirect_uri=self.oauth_redirect_uri,
                proxy=self.proxy,
                email_provider=self.email_provider,
                duckmail_api_base=self.duckmail_api_base,
                mail_gateway_base_url=self.mail_gateway_base_url,
                mail_gateway_token=self.mail_gateway_token,
                icloud_imap_host=self.icloud_imap_host,
                icloud_imap_port=self.icloud_imap_port,
                icloud_username=self.icloud_username,
                icloud_app_password=self.icloud_app_password,
            )
            if tokens:
                return tokens
            if attempt < attempts:
                backoff = min(self.oauth_retry_backoff_max, self.oauth_retry_backoff_base ** (attempt - 1))
                jitter = random.uniform(0.2, 0.8)
                time.sleep(backoff + jitter)
        return None


def register_one(runtime: RegisterRuntime, worker_id: int = 0) -> tuple[Optional[str], Optional[bool], float, float]:
    if runtime.stop_event.is_set() and runtime.get_token_success_count() >= runtime.target_tokens:
        return None, None, 0.0, 0.0

    t_start = time.time()
    session = create_session(proxy=runtime.proxy)

    if runtime.email_provider in ("mail_gateway", "icloud_gateway"):
        email, cf_token = create_temp_email_gateway(
            session,
            gateway_base_url=runtime.mail_gateway_base_url,
            gateway_token=runtime.mail_gateway_token,
            logger=runtime.logger,
        )
    elif runtime.email_provider == "icloud":
        email, cf_token = create_temp_email_icloud(
            email_domains=runtime.email_domains,
            logger=runtime.logger,
        )
    elif runtime.email_provider == "duckmail":
        email, cf_token = create_temp_email_duckmail(
            session,
            duckmail_api_base=runtime.duckmail_api_base,
            duckmail_bearer=runtime.duckmail_bearer,
            logger=runtime.logger,
        )
    else:
        email, cf_token = create_temp_email(
            session,
            worker_domain=runtime.worker_domain,
            email_domains=runtime.email_domains,
            admin_password=runtime.admin_password,
            logger=runtime.logger,
        )
    if not email or not cf_token:
        return None, False, 0.0, time.time() - t_start

    password = generate_random_password()
    registrar = ProtocolRegistrar(proxy=runtime.proxy, logger=runtime.logger)
    reg_ok = registrar.register(
        email=email,
        worker_domain=runtime.worker_domain,
        cf_token=cf_token,
        password=password,
        client_id=runtime.oauth_client_id,
        redirect_uri=runtime.oauth_redirect_uri,
        email_provider=runtime.email_provider,
        duckmail_api_base=runtime.duckmail_api_base,
        mail_gateway_base_url=runtime.mail_gateway_base_url,
        mail_gateway_token=runtime.mail_gateway_token,
        icloud_imap_host=runtime.icloud_imap_host,
        icloud_imap_port=runtime.icloud_imap_port,
        icloud_username=runtime.icloud_username,
        icloud_app_password=runtime.icloud_app_password,
    )
    t_reg = time.time() - t_start
    if not reg_ok:
        runtime.logger.warning("注册流程失败: %s", email)
        return email, False, t_reg, time.time() - t_start

    tokens = runtime.oauth_login_with_retry(email=email, password=password, cf_token=cf_token)
    t_total = time.time() - t_start
    if not tokens:
        return email, False, t_reg, t_total

    claimed, current = runtime.claim_token_slot()
    if not claimed:
        return email, None, t_reg, t_total

    saved = runtime.save_tokens(email, tokens)
    if not saved:
        runtime.release_token_slot()
        return email, False, t_reg, t_total

    runtime.save_account(email, password)
    runtime.logger.info(
        "注册+OAuth 成功: %s | 注册 %.1fs + OAuth %.1fs = %.1fs | token %s/%s",
        email,
        t_reg,
        t_total - t_reg,
        t_total,
        current,
        runtime.target_tokens,
    )
    return email, True, t_reg, t_total


def run_batch_register(conf: Dict[str, Any], target_tokens: int, logger: logging.Logger) -> tuple[int, int, int]:
    if target_tokens <= 0:
        return 0, 0, 0

    email_provider = str(pick_conf(conf, "email", "provider", default="cloudflare") or "cloudflare").lower()
    if email_provider in ("mail_gateway", "icloud_gateway"):
        mail_gateway_cfg = conf.get("mail_gateway") if isinstance(conf.get("mail_gateway"), dict) else {}
        if not mail_gateway_cfg.get("base_url") or not mail_gateway_cfg.get("token"):
            logger.error("mail_gateway.base_url 或 mail_gateway.token 未配置。")
            return 0, 0, 0
    elif email_provider == "icloud":
        icloud_cfg = conf.get("icloud") if isinstance(conf.get("icloud"), dict) else {}
        if not icloud_cfg.get("username") or not icloud_cfg.get("app_password"):
            logger.error("icloud.username 或 icloud.app_password 未配置。")
            return 0, 0, 0
    elif email_provider == "duckmail":
        if not conf.get("duckmail_bearer", ""):
            logger.error("duckmail_bearer 未配置，无法创建 DuckMail 临时邮箱。")
            return 0, 0, 0
    else:
        if not pick_conf(conf, "email", "admin_password", default=""):
            logger.error("email.admin_password 未配置，无法创建临时邮箱。")
            return 0, 0, 0

    runtime = RegisterRuntime(conf=conf, target_tokens=target_tokens, logger=logger)
    workers = runtime.concurrent_workers

    logger.info(
        "开始补号: 目标 token=%s, 并发=%s, 邮箱提供者=%s",
        target_tokens,
        workers,
        runtime.email_provider,
    )
    if runtime.email_provider in ("mail_gateway", "icloud_gateway"):
        logger.info("Mail Gateway: %s", runtime.mail_gateway_base_url)
    elif runtime.email_provider == "icloud":
        logger.info("iCloud IMAP: %s@%s, 域名=%s", runtime.icloud_username, runtime.icloud_imap_host, ",".join(runtime.email_domains))
    elif runtime.email_provider == "duckmail":
        logger.info("DuckMail API: %s", runtime.duckmail_api_base)
    else:
        logger.info("Worker域名=%s, 邮箱后缀=%s", runtime.worker_domain, ",".join(runtime.email_domains))

    ok = 0
    fail = 0
    skip = 0
    attempts = 0
    reg_times: List[float] = []
    total_times: List[float] = []
    lock = threading.Lock()
    batch_start = time.time()

    if workers == 1:
        while runtime.get_token_success_count() < target_tokens:
            attempts += 1
            email, success, t_reg, t_total = register_one(runtime, worker_id=1)
            if success is True:
                ok += 1
                reg_times.append(t_reg)
                total_times.append(t_total)
            elif success is False:
                fail += 1
            else:
                skip += 1
            logger.info(
                "补号进度: token %s/%s | ✅%s ❌%s ⏭️%s | 用时 %.1fs",
                runtime.get_token_success_count(),
                target_tokens,
                ok,
                fail,
                skip,
                time.time() - batch_start,
            )
            if runtime.get_token_success_count() >= target_tokens:
                break
            time.sleep(random.randint(2, 6))
    else:
        def worker_task(task_index: int, worker_id: int):
            if task_index > 1:
                jitter = random.uniform(0.5, 2.0) * worker_id
                time.sleep(jitter)
            if runtime.stop_event.is_set() and runtime.get_token_success_count() >= target_tokens:
                return task_index, None, None, 0.0, 0.0
            email, success, t_reg, t_total = register_one(runtime, worker_id=worker_id)
            return task_index, email, success, t_reg, t_total

        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {}
        next_task_index = 1

        def submit_one() -> bool:
            nonlocal next_task_index
            remaining = target_tokens - runtime.get_token_success_count()
            if remaining <= 0:
                return False
            if len(futures) >= remaining:
                return False

            wid = ((next_task_index - 1) % workers) + 1
            fut = executor.submit(worker_task, next_task_index, wid)
            futures[fut] = next_task_index
            next_task_index += 1
            return True

        try:
            for _ in range(min(workers, target_tokens)):
                if not submit_one():
                    break

            while futures:
                if runtime.get_token_success_count() >= target_tokens:
                    runtime.stop_event.set()
                    break

                done_set, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED, timeout=1.0)
                if not done_set:
                    continue

                for fut in done_set:
                    _ = futures.pop(fut, None)
                    attempts += 1
                    try:
                        _, _, success, t_reg, t_total = fut.result()
                    except Exception:
                        success, t_reg, t_total = False, 0.0, 0.0

                    with lock:
                        if success is True:
                            ok += 1
                            reg_times.append(t_reg)
                            total_times.append(t_total)
                        elif success is False:
                            fail += 1
                        else:
                            skip += 1

                        logger.info(
                            "补号进度: token %s/%s | ✅%s ❌%s ⏭️%s | 用时 %.1fs",
                            runtime.get_token_success_count(),
                            target_tokens,
                            ok,
                            fail,
                            skip,
                            time.time() - batch_start,
                        )

                    if runtime.get_token_success_count() < target_tokens:
                        submit_one()
        finally:
            runtime.stop_event.set()
            for f in list(futures.keys()):
                f.cancel()
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

    synced = runtime.reconcile_account_outputs_from_tokens()
    elapsed = time.time() - batch_start
    avg_reg = (sum(reg_times) / len(reg_times)) if reg_times else 0
    avg_total = (sum(total_times) / len(total_times)) if total_times else 0
    logger.info(
        "补号完成: token=%s/%s, fail=%s, skip=%s, attempts=%s, elapsed=%.1fs, avg(注册)=%.1fs, avg(总)=%.1fs, 收敛账号=%s",
        runtime.get_token_success_count(),
        target_tokens,
        fail,
        skip,
        attempts,
        elapsed,
        avg_reg,
        avg_total,
        synced,
    )
    return runtime.get_token_success_count(), fail, synced


