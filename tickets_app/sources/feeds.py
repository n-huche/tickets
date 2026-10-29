"""Feeds de promoções (apenas SINAL: não entram nos combos; ficam em `./tickets promos` e no painel)."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import primp

from .base import PromoItem, Source, SourceBlocked, SourceError
from .browser import BrowserSource

PRICE_RE = re.compile(r"R\$\s*([\d.]+(?:,\d{2})?)")


def _norm(t: str) -> str:
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


def relevant(text: str) -> bool:
    t = _norm(text)
    return "rio de janeiro" in t or "rio-de-janeiro" in t or "recife" in t


def price_of(text: str) -> float | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _get(url: str) -> str:
    c = primp.Client(impersonate="chrome_145", timeout=30)
    r = c.get(url)
    if r.status_code in (403, 429):
        raise SourceBlocked(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    return r.text


class MelhoresDestinos(Source):
    name, label, mode, seller_kind = "melhores_destinos", "Melhores Destinos", "feed", "promo"
    note = "RSS https://www.melhoresdestinos.com.br/feed filtrado por Recife/Rio (sinal)."

    def fetch_feed(self):
        root = ET.fromstring(_get("https://www.melhoresdestinos.com.br/feed").encode())
        out = []
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            desc = it.findtext("description") or ""
            if relevant(title) or relevant(desc[:400]):
                out.append(PromoItem(title=title.strip(), url=(it.findtext("link") or "").strip(),
                                     published=it.findtext("pubDate"), price=price_of(title + " " + desc)))
        return out


class PassagensImperdiveis(Source):
    name, label, mode, seller_kind = "passagens_imperdiveis", "Passagens Imperdíveis", "feed", "promo"
    note = "Sem RSS; lê os cards da página inicial e filtra Recife/Rio (sinal)."

    def fetch_feed(self):
        from selectolax.lexbor import LexborHTMLParser
        base = "https://www.passagensimperdiveis.com.br/"
        doc = LexborHTMLParser(_get(base))
        seen, out = set(), []
        for a in doc.css("a[href]"):
            href = a.attributes.get("href") or ""
            text = " ".join(a.text(separator=" ").split())
            if len(text) < 30 or not (relevant(text) or relevant(href)):
                continue
            url = urljoin(base, href)
            if url in seen:
                continue
            seen.add(url)
            out.append(PromoItem(title=text[:200], url=url, price=price_of(text)))
        return out


class Voopter(BrowserSource):
    """Página de promoções saindo de Recife (cache de ofertas ida+volta). Sinal apenas."""
    name, label, mode, seller_kind = "voopter", "Voopter", "feed", "promo"
    note = "Busca do Voopter é SPA sem URL estável; usamos a página de promoções de Recife (Chrome headless), filtrando Rio."
    match = ()

    def fetch_feed(self):
        self._ensure()
        url = "https://voopter.com.br/passagens-aereas-promocionais/de-recife/REC"
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
            if resp and resp.status in (403, 429):
                raise SourceBlocked(f"HTTP {resp.status}")
            page.wait_for_timeout(8000)
            body = page.inner_text("body")
        finally:
            page.close()
            self.close()
        out, seen = [], set()
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if "rio de janeiro" in _norm(l):
                chunk = " ".join(lines[i:i + 8])
                key = chunk[:120]
                if key in seen:
                    continue
                seen.add(key)
                k = chunk.find("Preço")
                tag = hashlib.md5(chunk.encode()).hexdigest()[:10]  # 1 linha por oferta (mesma página)
                out.append(PromoItem(title=chunk[:200], url=f"{url}#{tag}", price=price_of(chunk[k:] if k >= 0 else chunk)))
        return out
