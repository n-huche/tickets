"""Configuração central do monitor de passagens REC <-> RIO.

Todas as regras da viagem ficam aqui. Altere e reinicie o serviço
(`./tickets service restart`) para aplicar.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DASH_DIR = ROOT / "dashboard"
DB_PATH = DATA_DIR / "tickets.db"
HEARTBEAT_PATH = DATA_DIR / "heartbeat.json"
ENV_PATH = ROOT / ".env"

# ---- Regras da viagem -------------------------------------------------------
ORIGIN = "REC"                      # Recife
RIO_AIRPORTS = ["GIG", "SDU"]       # Galeão e Santos Dumont
ADULTS = 1

# Janelas em que é permitido estar no Rio (inclusivas).
WINDOWS: list[tuple[date, date]] = [
    (date(2027, 1, 1), date(2027, 1, 15)),
    (date(2027, 1, 24), date(2027, 2, 5)),
    (date(2027, 2, 11), date(2027, 2, 28)),
]
MIN_STAY_DAYS = 11                  # volta - ida >= 11
MAX_STAY_DAYS = 15                  # volta - ida <= 15
EARLIEST_DEPARTURE = (4, 0)         # não decolar antes de 04:00 (hora local)
LATEST_ARRIVAL = (23, 0)            # não pousar depois de 23:00 (mesmo dia)
NONSTOP_ONLY = True

PRICE_TARGET_BRL = 1100.0           # alerta quando ida + volta <= este valor

# ---- Varredura --------------------------------------------------------------
SWEEP_INTERVAL_MIN = 30             # intervalo médio entre varreduras (minutos)
SWEEP_JITTER_MIN = 8                # +/- jitter aleatório (minutos)
QUERY_DELAY_S = (2.5, 6.0)          # pausa aleatória entre consultas à mesma fonte
FARE_MAX_AGE_H = 6                  # tarifa mais velha que isso não entra nas combinações
BLOCK_COOLDOWN_MIN = (30, 120)      # backoff (min) após bloqueio; dobra a cada bloqueio seguido
HEARTBEAT_STALE_S = 300             # heartbeat sem atualização há mais que isso = travado

DASHBOARD_PORT = int(os.environ.get("TICKETS_DASHBOARD_PORT", "8787"))


def load_env() -> dict[str, str]:
    """Lê /workspace/tickets/.env (KEY=VALUE) sem sobrescrever o ambiente."""
    vals: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            vals[k.strip()] = v
    for k, v in vals.items():
        os.environ.setdefault(k, v)
    return vals


def webhook_config() -> tuple[str | None, str | None]:
    vals = load_env()  # relido a cada alerta: editar o .env vale sem reiniciar o serviço
    url = vals.get("TICKETS_WEBHOOK_URL") or os.environ.get("TICKETS_WEBHOOK_URL") or None
    auth = vals.get("TICKETS_WEBHOOK_AUTH") or os.environ.get("TICKETS_WEBHOOK_AUTH") or None
    return url, auth
