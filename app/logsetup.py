"""Process log: one line for each record, to stderr (`docker logs`)."""
import logging
import sys
from typing import TextIO

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# At INFO, httpx writes each request URL. The Telegram URL holds the bot token
# (notify.py), so these loggers stay at WARNING. APScheduler at INFO repeats
# the job lines that scheduler.py writes.
QUIET_LOGGERS = ("httpx", "httpcore", "apscheduler")


class _StderrHandler(logging.StreamHandler):
    """Write to the stderr of this moment, not to the one of the import time.

    A test runner or a wrapper can replace `sys.stderr` after the import. A
    stored stream would then be a closed file. Same shape as the last-resort
    handler of the `logging` module.
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:
        return sys.stderr


def configure() -> None:
    """Set up the process log. A second call changes nothing.

    uvicorn's own loggers have their own handlers and do not propagate, so
    its lines are not changed and not written two times.
    """
    root = logging.getLogger()
    if not any(isinstance(h, _StderrHandler) for h in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
