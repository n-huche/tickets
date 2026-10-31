"""Registro das fontes. A ordem importa: a principal (Google Flights) vem primeiro."""
from __future__ import annotations

from .base import Source
from .browser_sites import Kayak, Kiwi, MaxMilhas, Milhas123, Momondo
from .feeds import MelhoresDestinos, PassagensImperdiveis, Voopter
from .google_flights import GoogleFlights
from .skyscanner import Skyscanner
from .unavailable import UNAVAILABLE

REGISTRY: list[type[Source]] = [
    GoogleFlights,                      # principal: HTTP puro, toda varredura, todas as datas
    Kayak, Momondo, Milhas123, MaxMilhas, Kiwi,   # Chrome headless, rodízio de datas
    Skyscanner,                         # Chrome com janela (Xvfb próprio), resolve 'pressione e segure'
    MelhoresDestinos, PassagensImperdiveis, Voopter,  # feeds de promoção (sinal)
    *UNAVAILABLE,                       # testadas e desativadas (motivo documentado)
]


def get_sources(include_disabled: bool = False) -> list[Source]:
    srcs = [cls() for cls in REGISTRY]
    return srcs if include_disabled else [s for s in srcs if s.enabled]
