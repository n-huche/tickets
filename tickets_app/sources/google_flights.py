"""Google Flights (fonte principal), via protobuf `tfs` do pacote fast-flights.

Lê o JSON embutido (script ds:1) da página de resultados. Junta os blocos
"melhores voos" e "outros voos". Moeda BRL, idioma pt-BR, gl=BR.
"""
from __future__ import annotations

import json

from fast_flights import FlightQuery, Passengers, create_query
from primp import Client
from selectolax.lexbor import LexborHTMLParser

from .base import SearchResult, Source, SourceBlocked, SourceError, flight

URL = "https://www.google.com/travel/flights"


def _t(v) -> str:
    v = [*(v or []), None, None]
    return f"{v[0] or 0:02d}:{v[1] or 0:02d}"


def _d(v) -> str | None:
    return f"{v[0]:04d}-{v[1]:02d}-{v[2]:02d}" if v and len(v) >= 3 else None


class GoogleFlights(Source):
    name = "google_flights"
    label = "Google Flights"
    seller_kind = "metabusca"
    note = "Fonte principal. Preço da tarifa mais barata exibida (Azul/GOL/LATAM)."

    def __init__(self) -> None:
        self.client = None

    def rio_codes(self) -> list[str]:
        # O código de cidade RIO no Google só retorna GIG; SDU precisa de consulta própria.
        return ["GIG", "SDU"]

    def _client(self) -> Client:
        if self.client is None:
            self.client = Client(impersonate="chrome_145", impersonate_os="macos",
                                 referer=True, cookie_store=True, timeout=30)
        return self.client

    def search(self, origin: str, dest: str, day: str) -> SearchResult:
        q = create_query(flights=[FlightQuery(date=day, from_airport=origin, to_airport=dest, max_stops=0)],
                         trip="one-way", passengers=Passengers(adults=1), language="pt-BR", currency="BRL")
        params = {**q.params(), "gl": "BR"}
        url = q.url() + "&gl=BR"
        try:
            res = self._client().get(URL, params=params)
        except Exception as e:  # rede
            raise SourceError(f"rede: {e}") from e
        status = res.status_code
        html = res.text
        if status in (403, 429) or "unusual traffic" in html or "/sorry/" in str(getattr(res, "url", "")):
            raise SourceBlocked(f"HTTP {status} / tráfego incomum")
        if status != 200:
            raise SourceError(f"HTTP {status}")
        node = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if node is None:
            if "consent.google" in html or "captcha" in html.lower():
                raise SourceBlocked("página de consentimento/captcha")
            raise SourceError("script ds:1 ausente (layout mudou?)")
        js = node.text()
        try:
            data = js.split("data:", 1)[1].rsplit(",", 1)[0]
            if data.endswith("errorHasStatus: true"):
                return SearchResult([], url, status)
            payload = json.loads(data)
        except Exception as e:
            raise SourceError(f"parse JSON: {e}") from e

        out: dict[str, dict] = {}
        for blk in (2, 3):
            try:
                rows = payload[blk][0] if payload[blk] else None
            except (IndexError, TypeError):
                rows = None
            for k in rows or []:
                try:
                    f = k[0]
                    price = k[1][0][1] if k[1] and k[1][0] else None
                    legs = f[2]
                    first, last = legs[0], legs[-1]
                    code, num = (first[22][0], first[22][1]) if first[22] else (f[0], "?")
                    fno = f"{code}{num}" if len(legs) == 1 else "+".join(f"{x[22][0]}{x[22][1]}" for x in legs)
                    fl = flight(
                        airline_code=code, airline_name=(f[1][0] if f[1] else code), flight_no=fno,
                        origin=first[3], dest=last[6], dep_date=_d(first[20]), dep_time=_t(first[8]),
                        arr_date=_d(last[21]), arr_time=_t(last[10]), price=price,
                        stops=len(legs) - 1, duration_min=f[9] if len(f) > 9 and isinstance(f[9], int) else None,
                        fare_note="tarifa mais barata exibida", link=url)
                except Exception:
                    continue
                if fl["price"] is None:
                    continue
                prev = out.get(fno)
                if prev is None or fl["price"] < prev["price"]:
                    out[fno] = fl
        return SearchResult(list(out.values()), url, status)

    def close(self) -> None:
        self.client = None
