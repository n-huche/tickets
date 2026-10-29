"""Interface comum das fontes."""
from __future__ import annotations

from dataclasses import dataclass, field


class SourceError(Exception):
    """Falha genérica (rede, parse, formato inesperado)."""


class SourceBlocked(SourceError):
    """A fonte bloqueou (captcha, 403/429, anti-bot). Entra em backoff."""


class SourceNotFeasible(SourceError):
    """Fonte não utilizável de forma automatizada (desativada até revisão)."""


@dataclass
class SearchResult:
    flights: list[dict] = field(default_factory=list)
    url: str | None = None
    http_status: int | None = None


@dataclass
class PromoItem:
    title: str
    url: str
    published: str | None = None
    price: float | None = None


class Source:
    #: identificador curto usado no banco e nos logs
    name: str = "base"
    #: nome amigável
    label: str = "Base"
    #: metabusca | ota | cia | revenda_milhas | promo
    seller_kind: str = "metabusca"
    #: "search" = consulta por data/trecho; "feed" = feed de promoções (sinal)
    mode: str = "search"
    #: ativada por padrão?
    enabled: bool = True
    #: explicação quando desativada / limitações
    note: str = ""
    #: pausa (s) entre consultas a esta fonte
    delay_s: tuple[float, float] = (2.5, 6.0)
    #: máximo de consultas por varredura (0 = todas). Fontes lentas fazem um rodízio das datas mais antigas.
    max_queries_per_sweep: int = 0

    def rio_codes(self) -> list[str]:
        """Códigos usados para o lado Rio. Padrão: código de cidade RIO."""
        return ["RIO"]

    def search(self, origin: str, dest: str, day: str) -> SearchResult:  # pragma: no cover
        raise NotImplementedError

    def fetch_feed(self) -> list[PromoItem]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


def flight(*, airline_code, airline_name, flight_no, origin, dest, dep_date, dep_time,
           arr_date, arr_time, price, stops=0, duration_min=None, fare_note=None, link=None, seller=None,
           seller_kind=None) -> dict:
    """Normaliza um voo. Horários 'HH:MM' locais, datas 'YYYY-MM-DD', preço em BRL."""
    return dict(airline_code=airline_code, airline_name=airline_name, flight_no=flight_no,
                origin=origin, dest=dest, dep_date=dep_date, dep_time=dep_time,
                arr_date=arr_date, arr_time=arr_time, price=round(float(price), 2) if price is not None else None,
                stops=stops, duration_min=duration_min, fare_note=fare_note, link=link,
                seller=seller, seller_kind=seller_kind)
