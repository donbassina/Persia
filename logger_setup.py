import logging
import sys

__all__ = ["get_logger"]

_FMT = "[%(asctime)s] %(levelname)s %(name)s – %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_FMT,
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
    force=True,
)


def get_logger(name: str = "samokat") -> logging.Logger:
    return logging.getLogger(name)
