"""Fontes de preço plugáveis. Cada fonte é um módulo com uma subclasse de Source.

Para adicionar uma fonte: crie um módulo aqui, implemente `search()` (ou
`fetch_feed()` para feeds de promoção) e registre a classe em REGISTRY.
"""
from .base import Source, SourceBlocked, SourceError, SourceNotFeasible  # noqa: F401
from .registry import REGISTRY, get_sources  # noqa: F401
