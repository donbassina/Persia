from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
import json
import sys

from dotenv import dotenv_values

from utils import RunContext
from logger_setup import get_logger

logger = get_logger("samokat.cfg")


SCHEMA: dict[str, tuple[type, Any]] = {
    "UA": (str, lambda v: isinstance(v, str) and len(v) > 10),
    "HEADLESS": (bool, lambda v: isinstance(v, bool)),
    "BLOCK_PATTERNS": (
        list,
        lambda v: isinstance(v, list)
        and all(isinstance(x, str) and x != "" for x in v),
    ),
    "HUMAN_DELAY_μ": (float, lambda v: isinstance(v, (int, float))),
    "HUMAN_DELAY_σ": (float, lambda v: isinstance(v, (int, float)) and v > 0),
    "TYPO_PROB": (float, lambda v: isinstance(v, (int, float)) and 0 <= v <= 0.3),
    "SCROLL_STEP": (dict, None),
    "SCROLL_TO_FORM": (dict, None),
    "RUN_TIMEOUT": (int, lambda v: isinstance(v, int) and 30 <= v <= 600),
}

for _k in (
    "WEBHOOK_TIMEOUT",
    "SELECT_ITEM_TIMEOUT",
    "PAGE_GOTO_TIMEOUT",
    "FORM_WRAPPER_TIMEOUT",
    "REDIRECT_TIMEOUT",
    "MODAL_SELECTOR_TIMEOUT",
):
    SCHEMA[_k] = (int, lambda v: isinstance(v, int) and v > 0)


def _check_scroll_step(v: Any) -> bool:
    """
    Более гибкая проверка: допускаем любые ключи.
    Требования:
      • v — словарь
      • каждое значение — список целых чисел (вот так)
    """
    if not isinstance(v, dict):
        return False
    for lst in v.values():
        if not (isinstance(lst, list) and all(isinstance(i, int) for i in lst)):
            return False
    return True


SCHEMA["SCROLL_STEP"] = (dict, _check_scroll_step)


def _check_scroll_to_form(v: Any) -> bool:
    """Return True if ``v`` is a valid SCROLL_TO_FORM dict."""
    if not isinstance(v, dict):
        return False
    required = {"TIMEOUT", "BLOCK_PAUSE", "FINE_STEPS", "MAX_ITERS"}
    if not required.issubset(v.keys()):
        return False
    if not isinstance(v["TIMEOUT"], (int, float)) or v["TIMEOUT"] <= 0:
        return False
    if not (
        isinstance(v["MAX_ITERS"], int) and v["MAX_ITERS"] > 0
    ):
        return False
    bp = v["BLOCK_PAUSE"]
    if not (
        isinstance(bp, list)
        and len(bp) == 2
        and all(isinstance(x, (int, float)) and x >= 0 for x in bp)
        and bp[0] <= bp[1]
    ):
        return False
    fs = v["FINE_STEPS"]
    if not (
        isinstance(fs, list)
        and len(fs) == 4
        and all(isinstance(x, int) and x > 0 for x in fs)
    ):
        return False
    return True

SCHEMA["SCROLL_TO_FORM"] = (dict, _check_scroll_to_form)

CFG: dict[str, Any] = {}


def _convert(val: Any, typ: type) -> Any:
    if typ is bool:
        if isinstance(val, bool):
            return val
        return str(val).lower() in {"1", "true", "yes", "on"}
    if typ is int:
        if isinstance(val, int):
            return val
        return int(val)
    if typ is float:
        if isinstance(val, (int, float)):
            return float(val)
        return float(val)
    if typ is list:
        if isinstance(val, str):
            return [] if val == "" else json.loads(val)
        if isinstance(val, list):
            return val
        raise ValueError
    if typ is dict:
        if isinstance(val, str):
            return {} if val == "" else json.loads(val)
        if isinstance(val, dict):
            return val
        raise ValueError
    if typ is str:
        return str(val)
    return val


def load_cfg(
    base_dir: Path,
    *,
    ctx: RunContext,
    env_file: Path | None = None,
    cli_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load and validate configuration."""

    defaults_path = base_dir / "config_defaults.json"
    try:
        with open(defaults_path, encoding="utf-8") as f:
            defaults = json.load(f)
    except Exception as e:
        logger.error(f"[FATAL] Bad config defaults: {e}")
        sys.exit(1)

    env_path = env_file or (base_dir / ".env")
    env_data = dotenv_values(env_path) if env_path.exists() else {}

    result: dict[str, Any] = dict(defaults)

    for src in (env_data, cli_overrides or {}):
        for k, v in src.items():
            if k not in defaults:
                logger.warning(f"[WARN] Unknown cfg key {k}")
                continue
            result[k] = v

    final: dict[str, Any] = {}

    for k, (typ, check_fn) in SCHEMA.items():
        if k not in result:
            logger.error(f"[FATAL] Bad config missing {k}")
            sys.exit(1)
        try:
            val = _convert(result[k], typ)
        except Exception:
            logger.error(f"[FATAL] Bad config {k}")
            sys.exit(1)
        if check_fn and not check_fn(val):
            logger.error(f"[FATAL] Bad config {k}")
            sys.exit(1)
        final[k] = val

    for k in [
        key for key in result.keys() if key.endswith("_TIMEOUT") and key not in final
    ]:
        try:
            val = _convert(result[k], int)
        except Exception:
            logger.error(f"[FATAL] Bad config {k}")
            sys.exit(1)
        if val <= 0:
            logger.error(f"[FATAL] Bad config {k}")
            sys.exit(1)
        final[k] = val

    for key in list(result.keys()):
        if key not in final:
            logger.warning(f"[WARN] Unknown cfg key {key} ignored")

    overrides = {k: final[k] for k in final if defaults.get(k) != final[k]}
    logger.info("CONFIG loaded ok, overrides: %s", overrides)
    if not CFG:
        CFG.update(final)
    return final


if __name__ == "__main__":
    print(
        json.dumps(
            load_cfg(Path(__file__).parent, ctx=RunContext()),
            indent=2,
            ensure_ascii=False,
        )
    )
