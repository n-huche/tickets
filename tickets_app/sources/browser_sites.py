"""Fontes via navegador headless: Kayak, Momondo, MaxMilhas, 123milhas, Kiwi.com."""
from __future__ import annotations

from datetime import date

from .base import flight
from .browser import BrowserSource, hhmm

RESELLER_PROVIDERS = {"AIR123MILHASDOM": "123milhas", "AIR123MILHAS": "123milhas", "MAXMILHASAIR": "MaxMilhas",
                      "MAXMILHAS": "MaxMilhas"}
AIRLINE_PROVIDERS = {"AD", "G3", "LA", "JJ"}
AIRLINE_NAMES = {"AD": "Azul", "G3": "GOL", "LA": "LATAM", "JJ": "LATAM"}


def _kind_for_provider(code: str) -> str:
    if code in RESELLER_PROVIDERS:
        return "revenda_milhas"
    if code in AIRLINE_PROVIDERS:
        return "cia"
    return "ota"


class _KayakFamily(BrowserSource):
    """Kayak e Momondo (mesmo motor). Mostram o preço de vários vendedores por voo
    (cias, Decolar, ViajaNet, 123milhas, MaxMilhas...). Gravamos o mais barato e quem vende."""
    seller_kind = "metabusca"
    domain = "www.kayak.com.br"
    path = "flights"
    match = ("/i/api/search/dynamic/flights/poll",)

    def build_url(self, origin, dest, day):
        return f"https://{self.domain}/{self.path}/{origin}-{dest}/{day}?sort=price_a&fs=stops=0"

    def ready(self, caps):
        return any(isinstance(j, dict) and j.get("status") == "complete" and j.get("results") is not None
                   for _, j in caps)

    def parse(self, caps, origin, dest, day, url):
        polls = [j for _, j in caps if isinstance(j, dict) and j.get("legs") is not None]
        if not polls:
            return []
        j = polls[-1]
        legs, segs, provs = j.get("legs") or {}, j.get("segments") or {}, j.get("providers") or {}
        airl = j.get("airlines") or {}
        out = {}
        for r in j.get("results") or []:
            if r.get("type") != "core" or not r.get("legs"):
                continue
            leg_id = r["legs"][0]["id"]
            leg = legs.get(leg_id) or {}
            sids = [s["id"] for s in leg.get("segments", [])]
            if len(sids) != 1:
                continue  # só voo direto (1 segmento)
            s = segs.get(sids[0]) or {}
            best = None
            for b in r.get("bookingOptions") or []:
                p = (b.get("displayPrice") or {}).get("price")
                if p is None:
                    continue
                if best is None or p < best[0]:
                    best = (p, b.get("providerCode") or "?")
            if not best:
                continue
            price, pcode = best
            dd, dt = hhmm(s.get("departure", ""))
            ad, at = hhmm(s.get("arrival", ""))
            code = s.get("airline")
            fno = f"{code}{s.get('flightNumber')}"
            seller = (provs.get(pcode) or {}).get("displayName") or pcode
            f = flight(airline_code=code, airline_name=(airl.get(code) or {}).get("name") or AIRLINE_NAMES.get(code, code),
                       flight_no=fno, origin=s.get("origin"), dest=s.get("destination"), dep_date=dd, dep_time=dt,
                       arr_date=ad, arr_time=at, price=price, stops=0, duration_min=s.get("duration"),
                       fare_note=f"mais barato no {self.label}: {seller}", link=url,
                       seller=f"{seller} (via {self.label})", seller_kind=_kind_for_provider(pcode))
            if fno not in out or price < out[fno]["price"]:
                out[fno] = f
        return list(out.values())


class Kayak(_KayakFamily):
    name, label = "kayak", "Kayak"
    domain = "www.kayak.com.br"
    note = "Chrome headless; inclui ofertas de Decolar, ViajaNet, 123milhas, MaxMilhas e das cias como vendedores."


class Momondo(_KayakFamily):
    name, label = "momondo", "Momondo"
    domain = "www.momondo.com.br"
    path = "flight-search"
    note = "Mesmo motor do Kayak (grupo Booking); Chrome headless."


class MaxMilhas(BrowserSource):
    name, label = "maxmilhas", "MaxMilhas"
    seller_kind = "revenda_milhas"
    match = ("bff-mall.maxmilhas.com.br/search/air-offer/offers/",)
    note = "Chrome headless; captura a API interna da busca (ofertas LATAM/GOL/Azul)."
    settle_s = 6

    def build_url(self, origin, dest, day):
        return f"https://www.maxmilhas.com.br/busca-passagens-aereas/OW/{origin}/{dest}/{day}/1/0/0/EC"

    def ready(self, caps):
        got = {u.rstrip("/").rsplit("/", 1)[-1] for u, _ in caps}
        return {"latam", "gol", "azul"} <= got

    def parse(self, caps, origin, dest, day, url):
        out = {}
        for _, j in caps:
            for o in (j.get("offers") or []) if isinstance(j, dict) else []:
                try:
                    price = o["priceDetails"]["totalPrices"]["total"]
                except (KeyError, TypeError):
                    continue
                for b in o.get("bounds") or []:
                    segs = b.get("segments") or []
                    if b.get("totalStops") != 0 or len(segs) != 1:
                        continue
                    s = segs[0]
                    code = s.get("marketingAirlineCode") or b.get("carrier")
                    fno = f"{code}{s.get('marketingFlightNumber')}"
                    dd, dt = hhmm(s["departure"]["dateTime"]) if s.get("departure") else hhmm(b["departure"]["dateTime"])
                    ad, at = hhmm(s["arrival"]["dateTime"]) if s.get("arrival") else hhmm(b["arrival"]["dateTime"])
                    so = (s.get("departure") or {}).get("locationCode") or b["departure"]["locationCode"]
                    sd = (s.get("arrival") or {}).get("locationCode") or b["arrival"]["locationCode"]
                    kind = "revenda_milhas" if o.get("type") == "miles" else "ota"
                    f = flight(airline_code=code, airline_name=AIRLINE_NAMES.get(code, code), flight_no=fno,
                               origin=so, dest=sd, dep_date=dd, dep_time=dt, arr_date=ad, arr_time=at, price=price,
                               stops=0, fare_note=f"MaxMilhas tarifa {o.get('type')}", link=url,
                               seller="MaxMilhas", seller_kind=kind)
                    if fno not in out or price < out[fno]["price"]:
                        out[fno] = f
        return list(out.values())


class Milhas123(BrowserSource):
    name, label = "123milhas", "123milhas"
    seller_kind = "revenda_milhas"
    match = ("123milhas.com/api/v3/flight/search",)
    note = "Chrome headless; captura a API interna /api/v3/flight/search (exige sessão da página)."
    wait_s = 60
    settle_s = 2

    def build_url(self, origin, dest, day):
        y, m, d = day.split("-")
        return (f"https://123milhas.com/v2/busca?de={origin}&para={dest}&adultos=1&criancas=0&bebes=0"
                f"&ida={d}-{m}-{y}&classe=3&is_university=0")

    def ready(self, caps):
        return any(isinstance(j, dict) and isinstance(j.get("data"), dict) and j["data"].get("FLIGHTS")
                   for _, j in caps)

    def parse(self, caps, origin, dest, day, url):
        out = {}
        for _, j in caps:
            data = j.get("data") if isinstance(j, dict) else None
            if not isinstance(data, dict):
                continue
            fl = data.get("FLIGHTS") or {}
            grp_price: dict[str, float] = {}
            for g in data.get("GROUPS") or []:
                for oid in g.get("OUTBOUND") or []:
                    p = g.get("TOTAL_PRICE")
                    if p and (oid not in grp_price or p < grp_price[oid]):
                        grp_price[oid] = p
            for uid, x in fl.items():
                if x.get("QNT_STOP") or x.get("QNT_CONNECTION") or x.get("QNT_ESCALE") or len(x.get("STOPS") or []) != 1:
                    continue
                price = min(p for p in (grp_price.get(uid), x.get("TOTAL_PRICE")) if p)
                fno = (x.get("STANDARDIZED_FLIGHT_NUMBER") or x.get("FLIGHT_NUMBER") or "").replace("-", "")
                code = fno[:2]
                dd, dt = hhmm(x["DEPARTURE_TIME"])
                ad, at = hhmm(x["ARRIVAL_TIME"])
                f = flight(airline_code=code, airline_name=AIRLINE_NAMES.get(code, code), flight_no=fno,
                           origin=x.get("DEPARTURE_LOCATION"), dest=x.get("ARRIVAL_LOCATION"), dep_date=dd, dep_time=dt,
                           arr_date=ad, arr_time=at, price=price, stops=0,
                           fare_note="bagagem de mão incluída" if x.get("HAND_LUGGAGE") else None, link=url,
                           seller="123milhas", seller_kind="revenda_milhas")
                if fno not in out or price < out[fno]["price"]:
                    out[fno] = f
        return list(out.values())


class Kiwi(BrowserSource):
    name, label = "kiwi", "Kiwi.com"
    seller_kind = "ota"
    match = ("api.skypicker.com/umbrella/v2/graphql",)
    note = "Chrome headless; captura o GraphQL da busca. Preço em BRL (currency=brl)."
    settle_s = 4

    def build_url(self, origin, dest, day):
        o = "recife-pernambuco-brasil" if origin == "REC" else "rio-de-janeiro-rio-de-janeiro-brasil"
        d = "recife-pernambuco-brasil" if dest == "REC" else "rio-de-janeiro-rio-de-janeiro-brasil"
        return f"https://www.kiwi.com/br/search/results/{o}/{d}/{day}/no-return?stopNumber=0~true&sortBy=price&currency=brl"

    def ready(self, caps):
        for _, j in caps:
            it = ((j or {}).get("data") or {}).get("onewayItineraries") if isinstance(j, dict) else None
            if it and it.get("itineraries") is not None and not (it.get("metadata") or {}).get("hasMorePending"):
                return True
        return False

    def parse(self, caps, origin, dest, day, url):
        out = {}
        for _, j in caps:
            it = ((j or {}).get("data") or {}).get("onewayItineraries") if isinstance(j, dict) else None
            for x in (it or {}).get("itineraries") or []:
                segs = (x.get("sector") or {}).get("sectorSegments") or []
                if len(segs) != 1:
                    continue
                s = segs[0]["segment"]
                price = float((x.get("price") or {}).get("amount") or 0) or None
                code = (s.get("carrier") or {}).get("code")
                fno = f"{code}{s.get('code')}"
                dd, dt = hhmm(s["source"]["localTime"])
                ad, at = hhmm(s["destination"]["localTime"])
                f = flight(airline_code=code, airline_name=(s.get("carrier") or {}).get("name"), flight_no=fno,
                           origin=s["source"]["station"]["code"], dest=s["destination"]["station"]["code"],
                           dep_date=dd, dep_time=dt, arr_date=ad, arr_time=at, price=price, stops=0,
                           duration_min=(s.get("duration") or 0) // 60 or None, link=url, seller="Kiwi.com")
                if price and (fno not in out or price < out[fno]["price"]):
                    out[fno] = f
        return list(out.values())
