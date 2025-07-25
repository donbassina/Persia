"""Utilities: RunContext container and simple logging helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import os
import sys

__all__ = ["RunContext", "log", "make_log_file", "version_check"]


@dataclass(slots=True)
class RunContext:
    """Container for runtime state shared across functions."""

    cli_overrides: dict[str, str] | None = None
    cli_proxy: str | None = None
    proxy_url: str | None = None
    log_file: str | None = None
    log_start_pos: int = 0
    json_headless: bool | None = None
    screenshot_path: str = ""
    postback: str | None = None
    browser_closed_manually: bool = False
    first_abort_logged: bool = False

    def clone(self) -> "RunContext":
        """Return a deep copy of this context."""
        return deepcopy(self)


def make_log_file(logs_dir: str, phone: str) -> str:
    """Return a unique log file path inside *logs_dir* for *phone*."""
    date_str = datetime.now().strftime("%d.%m.%Y")
    base = f"{date_str}-{phone}"
    idx = 0
    while True:
        suffix = "" if idx == 0 else f"({idx})"
        path = os.path.join(logs_dir, f"{base}{suffix}.txt")
        if not os.path.exists(path):
            return path
        idx += 1


def log(msg: str, ctx: RunContext | None = None) -> None:
    """Write *msg* to ``ctx.log_file`` or stderr if context is missing."""
    ts_txt = f"{datetime.now()}  {msg}"
    if ctx and ctx.log_file:
        with open(ctx.log_file, "a", encoding="utf-8") as f:
            f.write(ts_txt + "\n")
    else:
        print(ts_txt, file=sys.stderr)


def version_check(required: dict[str, tuple[str, str]]) -> None:
    """Exit if installed packages do not satisfy *required* version ranges."""
    from importlib.metadata import PackageNotFoundError, version

    for pkg, (lo, hi) in required.items():
        try:
            v = version(pkg)
        except PackageNotFoundError:
            sys.exit(f"[FATAL] package {pkg} not installed")
        if not (lo <= v < hi):
            sys.exit(f"[FATAL] {pkg} {v} not supported; need >={lo}, <{hi}")
