from __future__ import annotations

import argparse
from pathlib import Path

import requests

from .config import load_json, pick_conf, setup_logger
from .pool_cleaner import run_clean_401
from .runtime import run_batch_register
from .utils import get_candidates_count, parse_bool, set_ssl_verify


def parse_args() -> argparse.Namespace:
    project_dir = Path(__file__).resolve().parent.parent
    default_cfg = project_dir / "config.json"
    default_log_dir = project_dir / "logs"

    parser = argparse.ArgumentParser(description="账号池自动维护（三合一：清理+补号+收敛）")
    parser.add_argument("--config", default=str(default_cfg), help="统一配置文件路径")
    parser.add_argument(
        "--min-candidates",
        type=int,
        default=None,
        help="候选账号最小阈值（默认读取 maintainer.min_candidates / 顶层 min_candidates，最终默认 100）",
    )
    parser.add_argument("--timeout", type=int, default=15, help="统计 candidates 时接口超时秒数")
    parser.add_argument("--log-dir", default=str(default_log_dir), help="日志目录")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()
    logger, log_path = setup_logger(Path(args.log_dir).resolve())
    logger.info("=== 账号池自动维护开始（二合一）===")
    logger.info("配置文件: %s", config_path)
    logger.info("日志文件: %s", log_path)

    if not config_path.exists():
        logger.error("配置文件不存在: %s", config_path)
        return 2

    conf = load_json(config_path)

    ssl_verify = parse_bool(pick_conf(conf, "run", "ssl_verify", default=False), default=False)
    set_ssl_verify(ssl_verify)
    if not ssl_verify:
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    base_url = str(pick_conf(conf, "clean", "base_url", default="") or "").rstrip("/")
    token = str(pick_conf(conf, "clean", "token", "cpa_password", default="") or "").strip()
    target_type = str(pick_conf(conf, "clean", "target_type", default="codex") or "codex")

    cfg_min_candidates = pick_conf(conf, "maintainer", "min_candidates", default=None)
    if cfg_min_candidates is None:
        cfg_min_candidates = conf.get("min_candidates")

    if args.min_candidates is not None:
        min_candidates = int(args.min_candidates)
    elif cfg_min_candidates is not None:
        min_candidates = int(cfg_min_candidates)
    else:
        min_candidates = 100

    if min_candidates < 0:
        logger.error("min_candidates 不能小于 0（当前值=%s）", min_candidates)
        return 2
    if not base_url or not token:
        logger.error("缺少 clean.base_url 或 clean.token/cpa_password")
        return 2

    try:
        probed_401, deleted_ok, deleted_fail = run_clean_401(conf, logger)
        logger.info("清理阶段汇总: 401命中=%s, 删除成功=%s, 删除失败=%s", probed_401, deleted_ok, deleted_fail)
    except Exception as e:
        logger.error("清理 401 失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 3

    try:
        total_after_clean, candidates_after_clean = get_candidates_count(
            base_url=base_url,
            token=token,
            target_type=target_type,
            timeout=args.timeout,
        )
    except Exception as e:
        logger.error("删除后统计失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 4

    logger.info(
        "删除401后统计: 总账号=%s, candidates=%s, 阈值=%s",
        total_after_clean,
        candidates_after_clean,
        min_candidates,
    )

    if candidates_after_clean >= min_candidates:
        logger.info("当前 candidates 已达标，无需补号。")
        logger.info("=== 账号池自动维护结束（成功）===")
        return 0

    gap = min_candidates - candidates_after_clean
    logger.info("当前 candidates 未达标，缺口=%s，开始补号。", gap)

    try:
        filled, failed, synced = run_batch_register(conf=conf, target_tokens=gap, logger=logger)
        logger.info("补号阶段汇总: 成功token=%s, 失败=%s, 收敛账号=%s", filled, failed, synced)
    except Exception as e:
        logger.error("补号阶段失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 5

    try:
        total_final, candidates_final = get_candidates_count(
            base_url=base_url,
            token=token,
            target_type=target_type,
            timeout=args.timeout,
        )
    except Exception as e:
        logger.error("补号后统计失败: %s", e)
        logger.info("=== 账号池自动维护结束（失败）===")
        return 6

    logger.info(
        "补号后统计: 总账号=%s, codex账号=%s, codex目标=%s",
        total_final,
        candidates_final,
        min_candidates,
    )
    if candidates_final < min_candidates:
        logger.warning("最终 codex账号数 仍低于阈值，请检查邮箱/OAuth/上传链路。")
    logger.info("=== 账号池自动维护结束（成功）===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
