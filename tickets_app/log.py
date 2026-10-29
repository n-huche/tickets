"""Logs estruturados (JSON por linha) com rotação, mais saída legível."""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime

from . import config as C

_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(r.created).astimezone().isoformat(timespec="seconds"),
             "level": r.levelname, "event": r.getMessage()}
        extra = getattr(r, "fields", None)
        if extra:
            d.update(extra)
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(r.created).strftime("%Y-%m-%d %H:%M:%S")
        extra = getattr(r, "fields", None) or {}
        kv = " ".join(f"{k}={v}" for k, v in extra.items())
        s = f"{ts} {r.levelname:<7} {r.getMessage()} {kv}".rstrip()
        if r.exc_info:
            s += "\n" + self.formatException(r.exc_info)
        return s


def get(stdout: bool = False) -> logging.Logger:
    global _configured
    lg = logging.getLogger("tickets")
    if not _configured:
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        lg.setLevel(logging.INFO)
        jh = logging.handlers.RotatingFileHandler(C.LOG_DIR / "tickets.jsonl", maxBytes=5_000_000,
                                                  backupCount=10, encoding="utf-8")
        jh.setFormatter(JsonFormatter())
        hh = logging.handlers.RotatingFileHandler(C.LOG_DIR / "tickets.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        hh.setFormatter(HumanFormatter())
        lg.addHandler(jh)
        lg.addHandler(hh)
        if stdout:
            sh = logging.StreamHandler()
            sh.setFormatter(HumanFormatter())
            lg.addHandler(sh)
        lg.propagate = False
        _configured = True
    return lg


def ev(lg: logging.Logger, level: int, event: str, **fields) -> None:
    lg.log(level, event, extra={"fields": fields})
