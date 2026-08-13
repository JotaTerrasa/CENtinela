"""Conectores de fuentes regulatorias publicas de Chile.

Los atributos se cargan de forma perezosa para que ``python -m
scrapers.chile_regulatory`` no preimporte dos veces el modulo ejecutable.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "SOURCE_REGISTRY",
    "ChileRegulatoryScraper",
    "RegulatoryDocument",
    "ScraperConfig",
    "ScraperError",
    "SourceDefinition",
    "fetch_regulatory_updates",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(".chile_regulatory", __name__)
    return getattr(module, name)
