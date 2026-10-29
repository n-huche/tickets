"""Fontes pedidas, testadas e desativadas (motivo em `note`; aparecem em `./tickets sources`)."""
from __future__ import annotations

from .base import Source


def _stub(name_, label_, kind, why):
    return type(f"Stub_{name_}", (Source,), dict(name=name_, label=label_, seller_kind=kind, enabled=False, note=why))


UNAVAILABLE = [
    _stub("skyscanner", "Skyscanner", "metabusca", "Bloqueado: captcha PerimeterX ('are you a person or a robot') já na 1ª busca headless."),
    _stub("azul", "Azul (site/promo)", "cia", "Bloqueado: HTTP 403 Akamai no site e na página de promoções. Tarifas Azul chegam via Google/Kayak/123milhas/MaxMilhas."),
    _stub("gol", "GOL (site/promo)", "cia", "Bloqueado: Akamai; a busca não carrega em headless. Tarifas GOL chegam via Google/Kayak/123milhas/MaxMilhas."),
    _stub("latam", "LATAM (site/promo)", "cia", "Bloqueado: reCAPTCHA Enterprise / 'a busca está demorando mais que o normal'. Tarifas LATAM chegam via Google/Kayak/123milhas/MaxMilhas."),
    _stub("decolar", "Decolar", "ota", "Bloqueado: HTTP 403 + captcha Cloudflare. Preços da Decolar aparecem como vendedor no Kayak/Momondo."),
    _stub("viajanet", "ViajaNet", "ota", "Bloqueado: HTTP 403 DataDome. Preços da ViajaNet aparecem como vendedor no Kayak/Momondo."),
    _stub("submarino", "Submarino Viagens", "ota", "Inviável: conexão TLS recusada (reset) a partir da box."),
    _stub("hurb", "Hurb", "ota", "Inviável: conexão TLS recusada (reset) a partir da box; foco em pacotes."),
    _stub("zarpo", "Zarpo", "ota", "Inviável: só vende hotéis/resorts, não passagens avulsas."),
]
