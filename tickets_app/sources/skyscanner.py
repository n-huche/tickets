"""Skyscanner (skyscanner.com.br) via Chrome com janela (Xvfb próprio) e perfil persistente.

O Skyscanner usa PerimeterX. Em headless a 1ª busca cai no captcha "Pressione e segure".
Com Chrome real (channel=chrome) em modo com janela, num Xvfb privado, e um perfil persistente,
o desafio é resolvido segurando o botão com o mouse; o cookie fica no perfil e as buscas seguintes
passam direto. Capturamos o JSON que a própria página carrega (/g/radar/api/v2/web-unified-search).
"""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import time

from ..config import DATA_DIR
from .base import SearchResult, SourceBlocked, SourceError, SourceNotFeasible, flight
from .browser import PAGE_SLOTS, BrowserSource, hhmm

PROFILE_DIR = DATA_DIR / "skyscanner-profile"
CITY = {"REC": "rec", "RIO": "rioa", "GIG": "gig", "SDU": "sdu"}


class Skyscanner(BrowserSource):
    name, label = "skyscanner", "Skyscanner"
    seller_kind = "metabusca"
    match = ("/g/radar/api/v2/web-unified-search",)
    note = ("Chrome com janela (Xvfb próprio) + perfil persistente; resolve o 'Pressione e segure' do "
            "PerimeterX segurando o mouse. Captura o JSON web-unified-search da página.")
    wait_s = 60
    settle_s = 2
    max_queries_per_sweep = 10
    delay_s = (6.0, 14.0)
    hold_attempts = 3

    def __init__(self) -> None:
        super().__init__()
        self._xvfb = None
        self._display = None

    def build_url(self, origin, dest, day):
        y, m, d = day.split("-")
        return (f"https://www.skyscanner.com.br/transporte/passagens-aereas/{CITY.get(origin, origin.lower())}/"
                f"{CITY.get(dest, dest.lower())}/{y[2:]}{m}{d}/?adultsv2=1&cabinclass=economy&childrenv2="
                f"&preferdirects=true&rtn=0&outboundaltsenabled=false&inboundaltsenabled=false")

    # --- navegador ---------------------------------------------------------------------------
    def _start_xvfb(self):
        if not shutil.which("Xvfb"):
            raise SourceNotFeasible("Xvfb não encontrado na box")
        for n in range(90, 130):
            if os.path.exists(f"/tmp/.X11-unix/X{n}") or os.path.exists(f"/tmp/.X{n}-lock"):
                continue
            p = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            if p.poll() is None:
                self._xvfb, self._display = p, f":{n}"
                return
        raise SourceNotFeasible("não consegui abrir um display Xvfb")

    def _ensure(self):
        if self._ctx:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise SourceNotFeasible("playwright não instalado no .venv") from e
        if not shutil.which("google-chrome"):
            raise SourceNotFeasible("google-chrome não encontrado na box")
        self._start_xvfb()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (PROFILE_DIR / lock).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False, env={**os.environ, "DISPLAY": self._display},
            locale="pt-BR", timezone_id="America/Sao_Paulo", viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-dev-shm-usage",
                  "--window-size=1366,1000"],
            ignore_default_args=["--enable-automation"])

    def _solve_hold(self, page) -> bool:
        """Segura o botão 'Pressione e segure' como uma pessoa. True se saiu do captcha."""
        for _ in range(self.hold_attempts):
            if "captcha" not in page.url:
                return True
            el = page.locator("#px-captcha")
            try:
                el.wait_for(timeout=15000)
                page.wait_for_timeout(random.randint(1500, 3000))
                bb = el.bounding_box()
            except Exception:
                bb = None
            if not bb:
                return False
            x = bb["x"] + bb["width"] / 2 + random.uniform(-15, 15)
            y = bb["y"] + bb["height"] / 2 + random.uniform(-5, 5)
            page.mouse.move(random.uniform(100, 300), random.uniform(500, 700))
            page.mouse.move(x, y, steps=random.randint(18, 30))
            page.wait_for_timeout(random.randint(200, 500))
            page.mouse.down()
            t_end = time.monotonic() + random.uniform(12, 16)
            while time.monotonic() < t_end:
                page.wait_for_timeout(400)
                page.mouse.move(x + random.uniform(-1, 1), y + random.uniform(-1, 1))
                if "captcha" not in page.url:
                    break
            page.mouse.up()
            for _ in range(15):
                page.wait_for_timeout(1000)
                if "captcha" not in page.url:
                    return True
        return "captcha" not in page.url

    def ready(self, caps):
        return any(isinstance(j, dict) and (j.get("context") or {}).get("status") == "complete" for _, j in caps)

    def search(self, origin, dest, day):
        self._ensure()
        url = self.build_url(origin, dest, day)
        with PAGE_SLOTS:
            page = self._ctx.new_page()
            pending: list = []
            page.on("response", lambda r: pending.append(r) if any(m in r.url for m in self.match) else None)
            try:
                resp, last = None, None
                for _ in range(4):  # a conexão TLS às vezes é fechada; tenta de novo
                    try:
                        resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        break
                    except Exception as e:
                        last = e
                        page.wait_for_timeout(3000)
                else:
                    raise SourceError(f"falha ao abrir página: {type(last).__name__}: {str(last)[:150]}")
                status = resp.status if resp else None
                caps: list[tuple[str, object]] = []
                t_end = time.monotonic() + self.wait_s
                ready_at = None
                solved = False
                while time.monotonic() < t_end:
                    page.wait_for_timeout(1000)
                    if "captcha" in page.url:
                        if solved or not self._solve_hold(page):
                            raise SourceBlocked("captcha PerimeterX não resolvido com 'pressione e segure'")
                        solved = True
                        t_end = time.monotonic() + self.wait_s
                        continue
                    while pending:
                        r = pending.pop(0)
                        try:
                            if r.status == 200:
                                caps.append((r.url, json.loads(r.text())))
                        except Exception:
                            pass
                    if ready_at is None and self.ready(caps):
                        ready_at = time.monotonic()
                    if ready_at and time.monotonic() - ready_at >= self.settle_s:
                        break
                if not caps:
                    raise SourceError(f"nenhum resultado capturado em {self.wait_s}s (título: {(page.title() or '')[:60]!r})")
                return SearchResult(flights=self.parse(caps, origin, dest, day, url), url=url, http_status=status)
            finally:
                try:
                    page.close()
                except Exception:
                    pass

    def parse(self, caps, origin, dest, day, url):
        out: dict[str, dict] = {}
        for _, j in caps:
            it = j.get("itineraries") if isinstance(j, dict) else None
            if not isinstance(it, dict):
                continue
            agents = {a.get("id"): a for a in it.get("agents") or [] if isinstance(a, dict)}
            for r in it.get("results") or []:
                legs = r.get("legs") or []
                if len(legs) != 1:
                    continue
                leg = legs[0]
                segs = leg.get("segments") or []
                if leg.get("stopCount") != 0 or len(segs) != 1:
                    continue
                s = segs[0]
                price = (r.get("price") or {}).get("raw")
                price = price if isinstance(price, (int, float)) and price > 0 else None
                agent_id = None
                for po in r.get("pricingOptions") or []:
                    amt = (po.get("price") or {}).get("amount")
                    if not isinstance(amt, (int, float)) or amt <= 0:
                        continue  # preço ainda não carregado (0/None) não vale
                    if price is None or amt <= price + 0.01:
                        price, agent_id = amt, (po.get("agentIds") or [None])[0]
                if price is None:
                    continue
                mc = s.get("marketingCarrier") or {}
                code = mc.get("alternateId") or mc.get("displayCode")
                fno = f"{code}{s.get('flightNumber')}"
                dd, dt = hhmm(s.get("departure", ""))
                ad, at = hhmm(s.get("arrival", ""))
                ag = agents.get(agent_id) or {}
                seller = ag.get("name") or agent_id or "?"
                kind = "cia" if ag.get("isCarrier") else "ota"
                if "123" in seller or "maxmilhas" in seller.lower():
                    kind = "revenda_milhas"
                f = flight(airline_code=code, airline_name=mc.get("name") or code, flight_no=fno,
                           origin=(s.get("origin") or {}).get("displayCode"),
                           dest=(s.get("destination") or {}).get("displayCode"),
                           dep_date=dd, dep_time=dt, arr_date=ad, arr_time=at, price=price, stops=0,
                           duration_min=s.get("durationInMinutes"), fare_note=f"mais barato no Skyscanner: {seller}",
                           link=url, seller=f"{seller} (via Skyscanner)", seller_kind=kind)
                if fno not in out or price < out[fno]["price"]:
                    out[fno] = f
        return list(out.values())

    def close(self) -> None:
        try:
            self._ctx and self._ctx.close()
        except Exception:
            pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass
        self._pw = self._ctx = None
        if self._xvfb:
            try:
                self._xvfb.terminate()
                self._xvfb.wait(timeout=5)
            except Exception:
                pass
            self._xvfb = self._display = None
