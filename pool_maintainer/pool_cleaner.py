from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from .constants import DEFAULT_MGMT_UA
from .config import pick_conf
from .utils import extract_chatgpt_account_id, get_item_type, mgmt_headers, safe_json_text

try:
    import aiohttp
except Exception:
    aiohttp = None

def fetch_auth_files(base_url: str, token: str, timeout: int) -> List[Dict[str, Any]]:
    resp = requests.get(f"{base_url}/v0/management/auth-files", headers=mgmt_headers(token), timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()
    data = raw if isinstance(raw, dict) else {}
    files = data.get("files", [])
    return files if isinstance(files, list) else []


def build_probe_payload(auth_index: str, user_agent: str, chatgpt_account_id: Optional[str] = None) -> Dict[str, Any]:
    call_header = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": user_agent or DEFAULT_MGMT_UA,
    }
    if chatgpt_account_id:
        call_header["Chatgpt-Account-Id"] = chatgpt_account_id
    return {
        "authIndex": auth_index,
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "header": call_header,
    }


async def probe_account_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    item: Dict[str, Any],
    user_agent: str,
    timeout: int,
    retries: int,
) -> Dict[str, Any]:
    auth_index = item.get("auth_index")
    name = item.get("name") or item.get("id")
    account = item.get("account") or item.get("email") or ""
    result = {
        "name": name,
        "account": account,
        "auth_index": auth_index,
        "type": get_item_type(item),
        "provider": item.get("provider"),
        "status_code": None,
        "invalid_401": False,
        "error": None,
    }
    if not auth_index:
        result["error"] = "missing auth_index"
        return result

    chatgpt_account_id = extract_chatgpt_account_id(item)
    payload = build_probe_payload(str(auth_index), user_agent, chatgpt_account_id)

    for attempt in range(retries + 1):
        try:
            async with semaphore:
                async with session.post(
                    f"{base_url}/v0/management/api-call",
                    headers={**mgmt_headers(token), "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(f"management api-call http {resp.status}: {text[:200]}")
                    data = safe_json_text(text)
                    sc = data.get("status_code")
                    result["status_code"] = sc
                    result["invalid_401"] = sc == 401
                    if sc is None:
                        result["error"] = "missing status_code in api-call response"
                    return result
        except Exception as e:
            result["error"] = str(e)
            if attempt >= retries:
                return result
    return result


async def delete_account_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    name: str,
    timeout: int,
) -> Dict[str, Any]:
    if not name:
        return {"name": None, "deleted": False, "error": "missing name"}
    encoded_name = quote(name, safe="")
    url = f"{base_url}/v0/management/auth-files?name={encoded_name}"
    try:
        async with semaphore:
            async with session.delete(url, headers=mgmt_headers(token), timeout=timeout) as resp:
                text = await resp.text()
                data = safe_json_text(text)
                ok = resp.status == 200 and data.get("status") == "ok"
                return {
                    "name": name,
                    "deleted": ok,
                    "status_code": resp.status,
                    "error": None if ok else f"delete failed, response={text[:200]}",
                }
    except Exception as e:
        return {"name": name, "deleted": False, "error": str(e)}


async def run_probe_async(
    base_url: str,
    token: str,
    target_type: str,
    workers: int,
    timeout: int,
    retries: int,
    user_agent: str,
    logger: Optional[logging.Logger] = None,
) -> tuple[List[Dict[str, Any]], int, int]:
    files = fetch_auth_files(base_url, token, timeout)
    candidates: List[Dict[str, Any]] = []
    for f in files:
        if str(get_item_type(f)).lower() != target_type.lower():
            continue
        candidates.append(f)

    if not candidates:
        return [], len(files), 0

    connector = aiohttp.TCPConnector(limit=max(1, workers), limit_per_host=max(1, workers))
    client_timeout = aiohttp.ClientTimeout(total=max(1, timeout))
    semaphore = asyncio.Semaphore(max(1, workers))

    probe_results = []
    total_candidates = len(candidates)
    checked = 0
    invalid_count = 0

    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=True) as session:
        tasks = [
            asyncio.create_task(
                probe_account_async(
                    session=session,
                    semaphore=semaphore,
                    base_url=base_url,
                    token=token,
                    item=item,
                    user_agent=user_agent,
                    timeout=timeout,
                    retries=retries,
                )
            )
            for item in candidates
        ]
        for task in asyncio.as_completed(tasks):
            result = await task
            probe_results.append(result)
            checked += 1
            if result.get("invalid_401"):
                invalid_count += 1

            if logger and (checked % 50 == 0 or checked == total_candidates):
                logger.info("401探测进度: 已检查=%s/%s, 命中401=%s", checked, total_candidates, invalid_count)

    invalid_401 = [r for r in probe_results if r.get("invalid_401")]
    return invalid_401, len(files), len(candidates)


async def run_delete_async(
    base_url: str,
    token: str,
    names_to_delete: List[str],
    delete_workers: int,
    timeout: int,
) -> tuple[int, int]:
    if not names_to_delete:
        return 0, 0

    connector = aiohttp.TCPConnector(limit=max(1, delete_workers), limit_per_host=max(1, delete_workers))
    client_timeout = aiohttp.ClientTimeout(total=max(1, timeout))
    semaphore = asyncio.Semaphore(max(1, delete_workers))

    delete_results = []
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=True) as session:
        tasks = [
            asyncio.create_task(
                delete_account_async(
                    session=session,
                    semaphore=semaphore,
                    base_url=base_url,
                    token=token,
                    name=name,
                    timeout=timeout,
                )
            )
            for name in names_to_delete
        ]
        for task in asyncio.as_completed(tasks):
            delete_results.append(await task)

    success = [r for r in delete_results if r.get("deleted")]
    failed = [r for r in delete_results if not r.get("deleted")]
    return len(success), len(failed)


async def run_clean_401_async(
    *,
    base_url: str,
    token: str,
    target_type: str,
    workers: int,
    delete_workers: int,
    timeout: int,
    retries: int,
    user_agent: str,
    logger: logging.Logger,
) -> tuple[int, int, int]:
    invalid_401, total_files, codex_files = await run_probe_async(
        base_url=base_url,
        token=token,
        target_type=target_type,
        workers=workers,
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        logger=logger,
    )
    names = [str(r.get("name")) for r in invalid_401 if r.get("name")]
    logger.info("探测完成: 总账号=%s, codex账号=%s, 401失效=%s", total_files, codex_files, len(names))

    deleted_ok, deleted_fail = await run_delete_async(
        base_url=base_url,
        token=token,
        names_to_delete=names,
        delete_workers=delete_workers,
        timeout=timeout,
    )
    logger.info("删除完成: 成功=%s, 失败=%s", deleted_ok, deleted_fail)
    return len(names), deleted_ok, deleted_fail


def run_clean_401(conf: Dict[str, Any], logger: logging.Logger) -> tuple[int, int, int]:
    if aiohttp is None:
        raise RuntimeError("未安装 aiohttp，请先安装: pip install aiohttp")

    base_url = str(pick_conf(conf, "clean", "base_url", default="") or "").rstrip("/")
    token = str(pick_conf(conf, "clean", "token", "cpa_password", default="") or "").strip()
    target_type = str(pick_conf(conf, "clean", "target_type", default="codex") or "codex")
    workers = int(pick_conf(conf, "clean", "workers", default=20) or 20)
    delete_workers = int(pick_conf(conf, "clean", "delete_workers", default=40) or 40)
    timeout = int(pick_conf(conf, "clean", "timeout", default=10) or 10)
    retries = int(pick_conf(conf, "clean", "retries", default=1) or 1)
    user_agent = str(pick_conf(conf, "clean", "user_agent", default=DEFAULT_MGMT_UA) or DEFAULT_MGMT_UA)

    if not base_url or not token:
        raise RuntimeError("clean 配置缺少 base_url 或 token/cpa_password")

    logger.info("开始清理 401: base_url=%s target_type=%s", base_url, target_type)
    return asyncio.run(
        run_clean_401_async(
            base_url=base_url,
            token=token,
            target_type=target_type,
            workers=workers,
            delete_workers=delete_workers,
            timeout=timeout,
            retries=retries,
            user_agent=user_agent,
            logger=logger,
        )
    )
