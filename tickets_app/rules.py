"""Regras de validade de voos e geração de combinações ida+volta."""
from __future__ import annotations

from datetime import date, timedelta

from . import config as C


def window_of(d: date) -> int | None:
    for i, (a, b) in enumerate(C.WINDOWS):
        if a <= d <= b:
            return i
    return None


def candidate_pairs(today: date | None = None) -> list[tuple[date, date, int]]:
    """Todos os pares (ida, volta, janela) válidos, ignorando datas passadas."""
    today = today or date.today()
    pairs = []
    for wi, (a, b) in enumerate(C.WINDOWS):
        d = a
        while d <= b:
            if d > today:
                for stay in range(C.MIN_STAY_DAYS, C.MAX_STAY_DAYS + 1):
                    r = d + timedelta(days=stay)
                    if r <= b:
                        pairs.append((d, r, wi))
            d += timedelta(days=1)
    return pairs


def candidate_dates(today: date | None = None) -> tuple[list[date], list[date]]:
    pairs = candidate_pairs(today)
    outs = sorted({p[0] for p in pairs})
    rets = sorted({p[1] for p in pairs})
    return outs, rets


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def check_flight(f: dict, query_date: str) -> str | None:
    """Retorna None se o voo é aceitável, ou o motivo da rejeição."""
    if C.NONSTOP_ONLY and int(f.get("stops", 0)) != 0:
        return "tem escala"
    if f.get("dep_date") and f["dep_date"] != query_date:
        return "data de partida diferente da consultada"
    if f.get("arr_date") and f.get("dep_date") and f["arr_date"] != f["dep_date"]:
        return "pousa no dia seguinte"
    if _hm(f["dep_time"]) < C.EARLIEST_DEPARTURE:
        return "decola antes de 04:00"
    if _hm(f["arr_time"]) > C.LATEST_ARRIVAL:
        return "pousa depois de 23:00"
    if f.get("price") is None or f["price"] <= 0:
        return "sem preço"
    return None
