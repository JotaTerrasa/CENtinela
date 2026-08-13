from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from agent.graph import RegulatoryAgent
from app import _billing_mode, provider_execution_metadata, provider_runtime_status
from core.config import PROJECT_ROOT, Settings
from core.observability import CostTrackingCallback
from core.providers import ProviderHealth
from rag.vector_engine import VectorEngine


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        chroma_path=tmp_path / "chroma",
        reports_path=tmp_path / "reports",
        codex_workdir=tmp_path / "codex-work",
        app_env="test",
        **overrides,
    )


def test_settings_route_models_by_provider_and_never_publish_keys(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        ai_provider="OLLAMA",
        report_provider="openai",
        embedding_provider="ollama",
        openai_api_key="sk-private",
        vllm_api_key="gateway-private",
    )

    assert settings.ai_provider == "ollama"
    assert settings.provider_for_role("filter") == "ollama"
    assert settings.model_for_role("filter") == "qwen3.5:9b"
    assert settings.provider_for_role("executor") == "openai"
    assert settings.model_for_role("executor") == "gpt-4o"
    assert settings.embedding_model_for_provider() == "qwen3-embedding:0.6b"
    public = json.dumps(settings.public_dict())
    assert "sk-private" not in public
    assert "gateway-private" not in public
    assert settings.public_dict()["secrets_configured"]["openai_api_key"] is True


@pytest.mark.parametrize(
    "endpoint",
    ("ollama:11434/v1", "ftp://ollama/v1", "http://user:pass@ollama/v1"),
)
def test_settings_reject_unsafe_provider_endpoints(tmp_path: Path, endpoint: str) -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        settings_for(tmp_path, ollama_base_url=endpoint)


def test_agent_factory_receives_effective_ollama_role(monkeypatch: Any, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, ai_provider="ollama")
    captured: dict[str, Any] = {}
    marker = object()

    def fake_factory(provider: str, **kwargs: Any) -> object:
        captured.update({"provider": provider, **kwargs})
        return marker

    monkeypatch.setattr("core.providers.create_generation_client", fake_factory)
    agent = RegulatoryAgent(
        settings=settings,
        database=SimpleNamespace(),
        scraper=SimpleNamespace(),
    )

    assert agent._get_llm("executor") is marker
    assert captured["provider"] == "ollama"
    assert captured["model"] == "qwen3.5:9b"
    assert captured["base_url"] == "http://127.0.0.1:11434/v1"
    assert captured["api_key"] is None


def test_http_profiles_activate_planner_and_filter_models(monkeypatch: Any, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, ai_provider="openai", openai_api_key="secret")
    responses = {
        "planner": SimpleNamespace(
            content=(
                '{"objective":"Vigilar BESS","lookback_days":5,'
                '"keywords":["BESS"],"max_items_per_source":4,'
                '"rationale":"Riesgo regulatorio"}'
            )
        ),
        "filter": SimpleNamespace(content='{"keep":[1]}'),
    }

    class Client:
        def __init__(self, role: str) -> None:
            self.role = role
            self.calls = 0

        def invoke(self, prompt: str, config: Any | None = None) -> Any:
            self.calls += 1
            return responses[self.role]

    clients = {role: Client(role) for role in responses}
    agent = RegulatoryAgent(
        settings=settings,
        database=SimpleNamespace(),
        scraper=SimpleNamespace(),
    )
    monkeypatch.setattr(agent, "_get_llm", lambda role: clients[role])
    state = {
        "execution_id": "execution",
        "report_date": "2026-08-13",
        "request": "Informe diario",
        "keywords": [],
        "errors": [],
    }

    planned = agent.planner_node(state)  # type: ignore[arg-type]
    selected = agent._filter_with_llm(
        {**state, "plan": planned["plan"]},  # type: ignore[arg-type]
        [{"title": "Norma BESS", "summary": "almacenamiento", "source": "CNE"}],
    )

    assert planned["plan"]["mode"] == "llm"
    assert planned["plan"]["lookback_days"] == 5
    assert selected[0]["title"] == "Norma BESS"
    assert clients["planner"].calls == clients["filter"].calls == 1


def test_openai_gpt4o_omits_unsupported_reasoning_effort(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, ai_provider="openai", openai_api_key="secret")
    captured: dict[str, Any] = {}

    def fake_factory(provider: str, **kwargs: Any) -> object:
        captured.update({"provider": provider, **kwargs})
        return object()

    monkeypatch.setattr("core.providers.create_generation_client", fake_factory)
    agent = RegulatoryAgent(
        settings=settings,
        database=SimpleNamespace(),
        scraper=SimpleNamespace(),
    )

    agent._get_llm("executor")
    assert captured["model"] == "gpt-4o"
    assert captured["reasoning_effort"] is None


class _Collection:
    pass


class _ChromaClient:
    def get_or_create_collection(self, **kwargs: Any) -> _Collection:
        return _Collection()


def test_vector_engine_builds_remote_embedding_provider(
    monkeypatch: Any, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, embedding_provider="ollama")
    captured: dict[str, Any] = {}
    marker = SimpleNamespace(embedding_identity="ollama/qwen3-embedding:0.6b")

    def fake_embeddings(provider: str, **kwargs: Any) -> Any:
        captured.update({"provider": provider, **kwargs})
        return marker

    monkeypatch.setattr("core.providers.create_embeddings_client", fake_embeddings)
    engine = VectorEngine(settings=settings, client=_ChromaClient())

    assert engine._get_embeddings() is marker
    assert engine._embedding_identity() == "ollama/qwen3-embedding:0.6b"
    assert captured["model"] == "qwen3-embedding:0.6b"


def test_provider_health_and_execution_metadata_are_provider_aware(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, ai_provider="vllm")

    class Client:
        def health(self, **kwargs: Any) -> ProviderHealth:
            return ProviderHealth(
                provider="vllm",
                available=True,
                reachable=True,
                authenticated=None,
                model="Qwen/Qwen3.5-9B",
                model_available=True,
                endpoint="http://127.0.0.1:8000/v1",
                latency_seconds=0.02,
                detail="ok",
            )

    runtime = provider_runtime_status(settings, client_factory=lambda *args, **kwargs: Client())
    assert runtime["authenticated"] is None
    assert runtime["ready"] is True
    assert runtime["available"] is True
    assert runtime["endpoint_reachable"] is True
    assert runtime["provider"] == "vllm"
    metadata = provider_execution_metadata(settings, "filter")
    assert metadata == {
        "provider": "vllm",
        "billing_mode": "self_hosted",
        "cost_attribution": "external_compute",
    }
    assert _billing_mode({"model": "self-hosted/vllm/qwen", "metadata": metadata}) == (
        "self_hosted"
    )


def test_provider_health_does_not_confuse_reachable_with_model_ready(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, ai_provider="ollama")

    class Client:
        def health(self, **kwargs: Any) -> ProviderHealth:
            return ProviderHealth(
                provider="ollama",
                available=False,
                reachable=True,
                authenticated=None,
                model="qwen3.5:9b",
                model_available=False,
                endpoint="http://127.0.0.1:11434/v1",
                latency_seconds=0.01,
                detail="modelo ausente",
            )

    runtime = provider_runtime_status(settings, client_factory=lambda *args, **kwargs: Client())

    assert runtime["endpoint_reachable"] is True
    assert runtime["available"] is False
    assert runtime["ready"] is False


def test_codex_health_reports_the_requested_role_model(monkeypatch: Any, tmp_path: Path) -> None:
    settings = settings_for(tmp_path, ai_provider="codex")
    monkeypatch.setattr(
        "app.codex_runtime_status",
        lambda _settings: {
            "available": True,
            "authenticated": True,
            "reason": "ready",
        },
    )

    runtime = provider_runtime_status(settings, "filter")

    assert runtime["ready"] is True
    assert runtime["model"] == settings.filter_model


def test_observability_separates_api_price_and_self_hosted_compute(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path, self_hosted_compute_usd_per_hour=3.6)
    api = CostTrackingCallback(settings=settings, model="gpt-4o")
    api_usage = api.record_usage(
        1_000_000,
        1_000_000,
        model="gpt-4o",
        metadata={"provider": "openai_api", "billing_mode": "api"},
    )
    assert api_usage.cost_usd == pytest.approx(12.5)
    assert api_usage.cost_clp == pytest.approx(11_750)

    hosted = CostTrackingCallback(settings=settings, model="qwen")
    hosted_usage = hosted.record_usage(
        100,
        20,
        model="self-hosted/ollama/qwen",
        latency_seconds=10,
        metadata={"provider": "ollama", "billing_mode": "self_hosted"},
    )
    assert hosted_usage.cost_usd == hosted_usage.cost_clp == 0
    metadata = hosted.calls[0]["metadata"]
    assert metadata["api_cost_status"] == "not_applicable"
    assert metadata["estimated_compute_cost_usd"] == pytest.approx(0.01)
    assert metadata["estimated_compute_cost_clp"] == pytest.approx(9.4)


def test_golden_contract_cases_are_well_formed() -> None:
    cases = json.loads(
        (PROJECT_ROOT / "evals" / "golden_cases.json").read_text(encoding="utf-8")
    )
    assert len(cases) >= 3
    assert all(case["documents"] and case["required_terms"] for case in cases)
    assert all(
        str(document["url"]).startswith("https://")
        for case in cases
        for document in case["documents"]
    )


def test_ollama_compose_pins_a_qwen35_compatible_release() -> None:
    compose = (PROJECT_ROOT / "docker-compose.ollama.yml").read_text(encoding="utf-8")
    versions = re.findall(r"ollama/ollama:(\d+\.\d+\.\d+)", compose)

    assert "qwen3.5:9b" in compose
    assert len(versions) == 2
    assert all(tuple(map(int, version.split("."))) >= (0, 17, 1) for version in versions)
    assert compose.count("@sha256:") == 2
