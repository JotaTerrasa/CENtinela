from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Lock

import requests

from scrapers.chile_regulatory import ChileRegulatoryScraper, ScraperConfig


class FakeResponse:
    def __init__(self, text: str, url: str, content_type: str = "text/html") -> None:
        self.text = text
        self.url = url
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None


class DirectFailsProxyLives:
    headers: dict[str, str]

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.headers = {}
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout: float):
        self.calls.append((url, timeout))
        if url.startswith("https://r.jina.ai/"):
            return FakeResponse(self.markdown, url, "text/plain")
        raise requests.ConnectionError("fuente oficial temporalmente inaccesible")


def test_live_reading_proxy_is_last_resort_and_explicitly_labelled():
    markdown = """Title: Noticias SEC
    URL Source: https://www.sec.cl/categoria/noticias/
    [**SEC fiscaliza sistemas BESS conectados a la red**](https://www.sec.cl/sec-fiscaliza-sistemas-bess/)
    La Superintendencia verificó la seguridad de instalaciones de almacenamiento.
    """
    session = DirectFailsProxyLives(markdown)
    scraper = ChileRegulatoryScraper(
        config=ScraperConfig(timeout_seconds=7, max_items_per_source=2, hydrate_articles=False),
        session=session,
        now=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    results = scraper.scrape_source("sec")

    assert len(results) == 1
    assert results[0]["url"] == "https://www.sec.cl/sec-fiscaliza-sistemas-bess/"
    assert results[0]["is_fallback"] is True
    assert "Proxy de lectura en vivo" in results[0]["fallback_reason"]
    assert "BESS" in results[0]["topics"]
    assert [url for url, _ in session.calls][-1].startswith("https://r.jina.ai/")
    assert all(timeout == 7 for _, timeout in session.calls)


def test_fallback_summary_removes_proxy_and_markdown_noise():
    markdown = """Title: Noticias SEC
URL Source: https://www.sec.cl/categoria/noticias/
[**SEC publica balance de fiscalización eléctrica**](https://www.sec.cl/balance-fiscalizacion/ "Abrir noticia")
[…] ![imagen decorativa](https://www.sec.cl/logo.png)
La Superintendencia informó los resultados de su fiscalización.
"""
    scraper = ChileRegulatoryScraper(
        config=ScraperConfig(max_items_per_source=1, hydrate_articles=False),
        session=DirectFailsProxyLives(markdown),
    )

    result = scraper.scrape_source("sec")[0]

    assert result["summary"] == (
        "La Superintendencia informó los resultados de su fiscalización."
    )
    assert "Abrir noticia" not in result["content"]
    assert "[…]" not in result["content"]


def test_settings_and_per_call_limit_are_honoured_without_import_side_effects():
    class Settings:
        scraper_timeout_seconds = 3.5
        scraper_max_articles = 4

    scraper = ChileRegulatoryScraper(settings=Settings())

    assert scraper.config.timeout_seconds == 3.5
    assert scraper.config.max_items_per_source == 4
    assert scraper._validated_limit(None) == 4
    assert scraper._validated_limit(2) == 2


def test_session_has_retry_policy_and_identifiable_user_agent():
    scraper = ChileRegulatoryScraper(config=ScraperConfig(retries=3, hydrate_articles=False))
    adapter = scraper.session.get_adapter("https://")

    assert adapter.max_retries.total == 3
    assert 429 in adapter.max_retries.status_forcelist
    assert "CENtinela/1.0" in scraper.session.headers["User-Agent"]


def test_batch_exposes_sanitized_errors_per_source():
    class AlwaysFails:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: float):
            raise requests.ConnectionError("token=sk-private123456")

    scraper = ChileRegulatoryScraper(
        config=ScraperConfig(retries=0, hydrate_articles=False),
        session=AlwaysFails(),
    )
    assert scraper.fetch_all(sources=["sec"], max_per_source=1) == []
    assert "sec" in scraper.last_errors
    assert "private123456" not in scraper.last_errors["sec"]


def test_batch_scrapes_independent_sources_in_parallel():
    class ParallelProbe(ChileRegulatoryScraper):
        def __init__(self) -> None:
            super().__init__(
                config=ScraperConfig(
                    retries=0,
                    hydrate_articles=False,
                    max_workers=3,
                )
            )
            self.barrier = Barrier(3)
            self.lock = Lock()
            self.active = 0
            self.peak = 0

        def _scrape_source_documents(self, key: str, limit: int):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                self.barrier.wait(timeout=2)
                return []
            finally:
                with self.lock:
                    self.active -= 1

    scraper = ParallelProbe()

    assert scraper.scrape_all(sources=["cen", "cne", "sec"]) == []
    assert scraper.peak == 3
    assert scraper.last_errors == {}
