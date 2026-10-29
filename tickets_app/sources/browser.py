"""Base para fontes que exigem navegador (Chrome headless via Playwright).

Sites como Kayak, MaxMilhas, 123milhas e Kiwi montam os resultados com XHR/JSON autenticado
pela própria página. Abrimos a página de busca e capturamos as respostas JSON relevantes.
Cada fonte usa seu próprio Chrome (thread própria); um semáforo limita páginas simultâneas.
"""
from __future__ import annotations

import json
import shutil
import threading
import time

from .base import SearchResult, Source, SourceBlocked, SourceError, SourceNotFeasible

PAGE_SLOTS = threading.Semaphore(3)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
BLOCK_WORDS = ("captcha", "are you a robot", "você é um robô", "access denied", "acesso negado",
               "verify you are human", "unusual traffic", "px-captcha")


class BrowserSource(Source):
    delay_s = (4.0, 10.0)
    max_queries_per_sweep = 14     # rodízio: ~14 consultas (~8-12 min) por varredura; 44 datas em ~3 varreduras
    wait_s = 40                    # tempo máximo esperando os resultados
    settle_s = 3                   # espera extra após detectar "pronto"
    match: tuple[str, ...] = ()    # trechos de URL das respostas JSON a capturar

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self._profile = None

    # --- a implementar nas subclasses -------------------------------------------------------
    def build_url(self, origin: str, dest: str, day: str) -> str:
        raise NotImplementedError

    def ready(self, caps: list[tuple[str, object]]) -> bool:
        return bool(caps)

    def parse(self, caps: list[tuple[str, object]], origin: str, dest: str, day: str, url: str) -> list[dict]:
        raise NotImplementedError

    # --- infraestrutura ---------------------------------------------------------------------
    def _ensure(self):
        if self._ctx:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise SourceNotFeasible("playwright não instalado no .venv") from e
        if not shutil.which("google-chrome"):
            raise SourceNotFeasible("google-chrome não encontrado na box")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel="chrome", headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-dev-shm-usage"])
        self._ctx = self._browser.new_context(locale="pt-BR", timezone_id="America/Sao_Paulo", user_agent=UA,
                                              viewport={"width": 1366, "height": 900})
        self._ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

    def search(self, origin: str, dest: str, day: str) -> SearchResult:
        self._ensure()
        url = self.build_url(origin, dest, day)
        with PAGE_SLOTS:
            page = self._ctx.new_page()
            pending: list = []
            page.on("response", lambda r: pending.append(r) if any(m in r.url for m in self.match) else None)
            try:
                try:
                    resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
                except Exception as e:
                    raise SourceError(f"falha ao abrir página: {type(e).__name__}: {str(e)[:150]}")
                status = resp.status if resp else None
                if status in (403, 429):
                    raise SourceBlocked(f"HTTP {status} na página de busca")
                caps: list[tuple[str, object]] = []
                t_end = time.monotonic() + self.wait_s
                ready_at = None
                while time.monotonic() < t_end:
                    page.wait_for_timeout(1000)
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
                    title = (page.title() or "").lower()
                    try:
                        body = page.inner_text("body", timeout=3000).lower()[:20000]
                    except Exception:
                        body = ""
                    if any(w in title or w in body for w in BLOCK_WORDS):
                        raise SourceBlocked(f"anti-bot/captcha na página (título: {title[:60]!r})")
                    raise SourceError(f"nenhum resultado capturado em {self.wait_s}s (título: {title[:60]!r})")
                flights = self.parse(caps, origin, dest, day, url)
                return SearchResult(flights=flights, url=url, http_status=status)
            finally:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                obj and obj.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = None


def hhmm(iso: str) -> tuple[str, str]:
    """'2027-01-05T14:35:00[-03:00|Z]' -> ('2027-01-05', '14:35') usando o horário local escrito."""
    d, t = iso[:10], iso[11:16]
    return d, t
