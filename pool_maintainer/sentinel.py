from __future__ import annotations

import base64
import datetime as dt
import json
import random
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from .constants import USER_AGENT
from .utils import get_ssl_verify

class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id or str(uuid.uuid4())
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        h &= 0xFFFFFFFF
        return format(h, "08x")

    @staticmethod
    def _base64_encode(data: Any) -> str:
        js = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return base64.b64encode(js.encode("utf-8")).decode("ascii")

    def _get_config(self) -> List[Any]:
        now = dt.datetime.now(dt.timezone.utc).strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        return [
            "1920x1080",
            now,
            4294705152,
            random.random(),
            USER_AGENT,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "vendorSub−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time_origin,
        ]

    def _run_check(self, start_time: float, seed: str, difficulty: str, config: List[Any], nonce: int) -> Optional[str]:
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        if hash_hex[: len(difficulty)] <= difficulty:
            return data + "~S"
        return None

    def generate_requirements_token(self) -> str:
        cfg = self._get_config()
        cfg[3] = 1
        cfg[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(cfg)

    def generate_token(self, seed: Optional[str] = None, difficulty: Optional[str] = None) -> str:
        if seed is None:
            seed = self.requirements_seed
            difficulty = difficulty or "0"
        cfg = self._get_config()
        start = time.time()
        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start, seed, difficulty or "0", cfg, i)
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))


def fetch_sentinel_challenge(session: requests.Session, device_id: str, flow: str = "authorize_continue") -> Optional[Dict[str, Any]]:
    gen = SentinelTokenGenerator(device_id=device_id)
    body = {"p": gen.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "User-Agent": USER_AGENT,
        "Origin": "https://sentinel.openai.com",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    try:
        resp = session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            data=json.dumps(body),
            headers=headers,
            timeout=15,
            verify=get_ssl_verify(),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def build_sentinel_token(session: requests.Session, device_id: str, flow: str = "authorize_continue") -> Optional[str]:
    challenge = fetch_sentinel_challenge(session, device_id, flow)
    if not challenge:
        return None
    c_value = challenge.get("token", "")
    pow_data = challenge.get("proofofwork", {})
    gen = SentinelTokenGenerator(device_id=device_id)
    if isinstance(pow_data, dict) and pow_data.get("required") and pow_data.get("seed"):
        p_value = gen.generate_token(seed=pow_data.get("seed"), difficulty=pow_data.get("difficulty", "0"))
    else:
        p_value = gen.generate_requirements_token()
    return json.dumps({"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": flow})

