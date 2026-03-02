from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件格式错误，顶层必须是对象: {path}")
    return data


def setup_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"pool_maintainer_{ts}.log"

    logger = logging.getLogger("pool_maintainer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger, log_path


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def pick_conf(root: Dict[str, Any], section: str, key: str, *legacy_keys: str, default: Any = None) -> Any:
    sec = root.get(section)
    if not isinstance(sec, dict):
        sec = {}

    v = sec.get(key)
    if v is None:
        for lk in legacy_keys:
            v = sec.get(lk)
            if v is not None:
                break
    if v is not None:
        return v

    v = root.get(key)
    if v is None:
        for lk in legacy_keys:
            v = root.get(lk)
            if v is not None:
                break
    if v is not None:
        return v
    return default

