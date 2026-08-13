from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from rag.vector_engine import LocalHashEmbeddings, VectorEngine


URL = "https://www.cne.cl/normativa/almacenamiento?version=original"
SOURCE = "Comisión Nacional de Energía (CNE)"


class FakeCollection:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.events: list[tuple[str, tuple[str, ...]]] = []
        self.fail_upsert_after: int | None = None
        self.fail_delete = False
        self.last_query: dict[str, Any] | None = None

    def get(
        self,
        where: dict[str, Any] | None = None,
        include: Any | None = None,
    ) -> dict[str, Any]:
        selected = [
            (identifier, item)
            for identifier, item in self.items.items()
            if not where or all(item["metadata"].get(key) == value for key, value in where.items())
        ]
        return {
            "ids": [identifier for identifier, _ in selected],
            "documents": [item["document"] for _, item in selected],
            "metadatas": [item["metadata"] for _, item in selected],
        }

    def upsert(
        self,
        *,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        self.events.append(("upsert", tuple(ids)))
        for position, (identifier, document, metadata, embedding) in enumerate(zip(
            ids, documents, metadatas, embeddings, strict=True
        ), start=1):
            self.items[identifier] = {
                "document": document,
                "metadata": metadata,
                "embedding": embedding,
            }
            if self.fail_upsert_after == position:
                raise RuntimeError("fallo de upsert simulado")

    def delete(self, *, ids: list[str]) -> None:
        self.events.append(("delete", tuple(ids)))
        if self.fail_delete:
            raise RuntimeError("fallo de delete simulado")
        for identifier in ids:
            self.items.pop(identifier, None)

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.last_query = kwargs
        where = kwargs.get("where") or {}
        candidates = [
            (identifier, item)
            for identifier, item in self.items.items()
            if all(item["metadata"].get(key) == value for key, value in where.items())
        ]
        query_embedding = kwargs["query_embeddings"][0]

        def cosine_distance(item: tuple[str, dict[str, Any]]) -> float:
            embedding = item[1]["embedding"]
            dot_product = sum(
                left * right
                for left, right in zip(query_embedding, embedding, strict=True)
            )
            query_norm = math.sqrt(sum(value * value for value in query_embedding))
            item_norm = math.sqrt(sum(value * value for value in embedding))
            similarity = dot_product / (query_norm * item_norm) if query_norm and item_norm else 0.0
            return 1.0 - similarity

        selected = sorted(candidates, key=cosine_distance)[: int(kwargs["n_results"])]
        return {
            "ids": [[identifier for identifier, _ in selected]],
            "documents": [[item["document"] for _, item in selected]],
            "metadatas": [[item["metadata"] for _, item in selected]],
            "distances": [[cosine_distance(item) for item in selected]],
        }

    def count(self) -> int:
        return len(self.items)


class FakeClient:
    def __init__(self) -> None:
        self.collection = FakeCollection()

    def get_or_create_collection(self, **kwargs: Any) -> FakeCollection:
        return self.collection


class FakeEmbeddings:
    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return [float(len(text)), 1.0]


class BrokenQueryEmbeddings(FakeEmbeddings):
    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("sin servicio de embeddings")


class FakeLLM:
    def invoke(self, prompt: str, config: Any | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            content=f"La norma recuperada aborda almacenamiento. [{SOURCE} | {URL}]"
        )


class FakeCodexLLM:
    def invoke(self, prompt: str, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            text=f"La norma recuperada aborda almacenamiento. [{SOURCE} | {URL}]"
        )


class StructuredFakeCodexLLM:
    def invoke_json(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        **kwargs: Any,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            data={
                "claims": [
                    {
                        "text": "La norma recuperada aborda almacenamiento.",
                        "source_ids": [1],
                    }
                ]
            }
        )


class BrokenLLM:
    def invoke(self, prompt: str, config: Any | None = None) -> SimpleNamespace:
        raise RuntimeError("Codex no disponible temporalmente")


def settings(tmp_path: Any) -> SimpleNamespace:
    return SimpleNamespace(
        chroma_path=tmp_path / "chroma",
        codex_cli_path="codex-test",
        codex_timeout_seconds=42.0,
        codex_workdir=tmp_path / "codex-work",
        embedding_model="local-hash-1536",
        filter_model="gpt-5.6-luna",
        filter_reasoning_effort="low",
        planner_model="gpt-5.6-luna",
        rag_top_k=5,
    )


def document(content: str = "La norma establece exigencias técnicas para BESS.") -> dict[str, Any]:
    return {
        "title": "Norma técnica de almacenamiento",
        "summary": "La CNE publicó una norma para almacenamiento.",
        "content": content,
        "source": SOURCE,
        "url": URL,
        "source_url": "https://www.cne.cl/prensa/",
        "published_at": "2026-08-13",
        "topics": ["BESS", "almacenamiento"],
    }


def test_local_embeddings_are_deterministic_normalized_and_accent_insensitive() -> None:
    embeddings = LocalHashEmbeddings(dimensions=256)

    accented = embeddings.embed_query("Transmisión eléctrica y almacenamiento BESS")
    unaccented = embeddings.embed_query("transmision electrica y almacenamiento bess")
    unrelated = embeddings.embed_query("permisos ambientales para hidrogeno verde")

    assert accented == unaccented
    assert accented != unrelated
    assert len(accented) == 256
    assert math.sqrt(sum(value * value for value in accented)) == pytest.approx(1.0)


def test_default_rag_indexes_and_answers_with_codex_without_api_key(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import codex_client

    constructor_options: dict[str, Any] = {}

    def fake_codex_client(**kwargs: Any) -> FakeCodexLLM:
        constructor_options.update(kwargs)
        return FakeCodexLLM()

    monkeypatch.setattr(codex_client, "CodexClient", fake_codex_client)
    client = FakeClient()
    engine = VectorEngine(settings(tmp_path), client=client)

    indexed = engine.index_documents([document()])
    result = engine.ask("¿Qué exige la norma para BESS?")

    assert indexed["status"] == "completed"
    assert indexed["documents_indexed"] == 1
    assert isinstance(engine._get_embeddings(), LocalHashEmbeddings)
    assert result["mode"] == "local_vector+llm"
    assert result["error"] is None
    assert f"[{SOURCE} | {URL}]" in result["answer"]
    assert result["sources"][0]["url"] == URL
    stored = next(iter(client.collection.items.values()))
    assert stored["metadata"]["embedding_model"].startswith(
        LocalHashEmbeddings.VERSION
    )
    assert client.collection.last_query is not None
    assert client.collection.last_query["where"] == {
        "embedding_model": stored["metadata"]["embedding_model"]
    }
    assert constructor_options == {
        "executable": "codex-test",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "timeout_seconds": 42.0,
        "workdir": tmp_path / "codex-work",
    }


def test_local_retrieval_ranks_regulatory_topic_without_external_model(
    tmp_path: Any,
) -> None:
    client = FakeClient()
    engine = VectorEngine(settings(tmp_path), client=client)
    bess = document("BESS y baterías almacenan energía para estabilizar la red eléctrica.")
    hydrogen = {
        **document(
            "La evaluación ambiental regula electrolizadores y proyectos de hidrógeno verde."
        ),
        "title": "Permisos para hidrógeno verde",
        "url": "https://sea.gob.cl/hidrogeno-verde",
        "source": "Servicio de Evaluación Ambiental (SEA)",
        "topics": ["hidrógeno verde", "permisos"],
    }
    engine.index_documents([hydrogen, bess])

    result = engine.search("baterías BESS almacenamiento de energía", k=1)

    assert result["mode"] == "local_vector"
    assert result["error"] is None
    assert result["results"][0]["url"] == URL


def test_legacy_chunks_without_local_embedding_version_are_reindexed(
    tmp_path: Any,
) -> None:
    client = FakeClient()
    embeddings = FakeEmbeddings()
    engine = VectorEngine(settings(tmp_path), client=client, embeddings=embeddings)
    engine.index_documents([document()])
    for item in client.collection.items.values():
        item["metadata"].pop("embedding_model")

    migrated = engine.index_documents([document()])

    assert migrated["documents_indexed"] == 1
    assert migrated["documents_skipped"] == 0
    assert embeddings.document_calls == 2
    assert all(
        item["metadata"]["embedding_model"].endswith("FakeEmbeddings")
        for item in client.collection.items.values()
    )


def test_persistent_chroma_roundtrip_uses_local_embeddings(tmp_path: Any) -> None:
    configured = settings(tmp_path)
    first_engine = VectorEngine(
        configured,
        collection_name="centinela_local_integration",
    )
    indexed = first_engine.index_documents([document()], raise_on_error=True)

    reloaded_engine = VectorEngine(
        configured,
        collection_name="centinela_local_integration",
        llm=BrokenLLM(),
    )
    result = reloaded_engine.ask("almacenamiento técnico para BESS", k=1)

    assert indexed["documents_indexed"] == 1
    assert reloaded_engine.count() >= 1
    assert result["mode"] == "local_vector+extractive"
    assert "Codex no disponible" in str(result["error"])
    assert result["sources"][0]["url"] == URL
    assert f"[{SOURCE} | {URL}]" in result["answer"]


def test_persistent_chroma_disables_product_telemetry(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chromadb

    original_client = chromadb.PersistentClient
    captured: dict[str, Any] = {}

    def persistent_client(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(chromadb, "PersistentClient", persistent_client)
    VectorEngine(settings(tmp_path), collection_name="centinela_no_telemetry")

    chroma_settings = captured["settings"]
    assert chroma_settings.anonymized_telemetry is False


def test_index_is_idempotent_and_preserves_original_metadata(tmp_path: Any) -> None:
    embeddings = FakeEmbeddings()
    client = FakeClient()
    engine = VectorEngine(
        settings(tmp_path), client=client, embeddings=embeddings, llm=FakeLLM()
    )

    first = engine.index_news([document()])
    second = engine.index_documents([document()])

    assert first["documents_indexed"] == 1
    assert first["chunks_indexed"] >= 1
    assert second["documents_skipped"] == 1
    assert embeddings.document_calls == 1
    stored = next(iter(client.collection.items.values()))
    assert stored["metadata"]["source"] == SOURCE
    assert stored["metadata"]["url"] == URL
    assert stored["metadata"]["source_url"] == "https://www.cne.cl/prensa/"
    assert len(stored["metadata"]["document_id"]) == 64


def test_changed_document_replaces_stale_chunks(tmp_path: Any) -> None:
    client = FakeClient()
    engine = VectorEngine(
        settings(tmp_path),
        client=client,
        embeddings=FakeEmbeddings(),
        chunk_size=200,
        chunk_overlap=20,
    )
    engine.index_documents([document("texto anterior " * 80)])
    old_ids = set(client.collection.items)
    client.collection.events.clear()

    result = engine.index_documents([document("contenido actualizado " * 8)])

    assert result["documents_indexed"] == 1
    assert old_ids.isdisjoint(client.collection.items)
    assert [event[0] for event in client.collection.events] == ["upsert", "delete"]
    assert set(client.collection.events[1][1]) == old_ids


def test_failed_upsert_rolls_back_new_chunks_and_preserves_previous_version(
    tmp_path: Any,
) -> None:
    client = FakeClient()
    engine = VectorEngine(
        settings(tmp_path),
        client=client,
        embeddings=FakeEmbeddings(),
        chunk_size=200,
        chunk_overlap=20,
    )
    engine.index_documents([document("version anterior estable " * 60)])
    previous = {
        identifier: dict(item)
        for identifier, item in client.collection.items.items()
    }
    client.collection.events.clear()
    client.collection.fail_upsert_after = 1

    result = engine.index_documents([document("version nueva incompleta " * 25)])

    assert result["status"] == "error"
    assert result["documents_indexed"] == 0
    assert set(client.collection.items) == set(previous)
    assert all(
        client.collection.items[identifier]["document"] == item["document"]
        for identifier, item in previous.items()
    )
    assert [event[0] for event in client.collection.events] == ["upsert", "delete"]
    assert "fallo de upsert simulado" in " ".join(result["errors"])


def test_failed_stale_cleanup_keeps_new_and_previous_versions_available(
    tmp_path: Any,
) -> None:
    client = FakeClient()
    engine = VectorEngine(
        settings(tmp_path),
        client=client,
        embeddings=FakeEmbeddings(),
        chunk_size=200,
        chunk_overlap=20,
    )
    engine.index_documents([document("version anterior estable " * 60)])
    old_ids = set(client.collection.items)
    client.collection.events.clear()
    client.collection.fail_delete = True

    result = engine.index_documents([document("version nueva completa " * 25)])

    assert result["status"] == "partial"
    assert result["documents_indexed"] == 1
    assert old_ids.issubset(client.collection.items)
    assert set(client.collection.items) - old_ids
    assert [event[0] for event in client.collection.events] == ["upsert", "delete"]
    assert "limpieza de version anterior" in " ".join(result["errors"])


def test_ask_returns_cited_answer_and_structured_sources(tmp_path: Any) -> None:
    engine = VectorEngine(
        settings(tmp_path),
        client=FakeClient(),
        embeddings=FakeEmbeddings(),
        llm=FakeLLM(),
    )
    engine.index_documents([document()])

    result = engine.ask("¿Qué cambió para BESS?", k=3)

    assert f"[{SOURCE} | {URL}]" in result["answer"]
    assert result["sources"] == [
        {"source": SOURCE, "url": URL, "title": "Norma técnica de almacenamiento"}
    ]
    assert result["mode"] == "local_vector+llm"


def test_structured_codex_answer_maps_ids_to_verified_citations(tmp_path: Any) -> None:
    engine = VectorEngine(
        settings(tmp_path),
        client=FakeClient(),
        embeddings=FakeEmbeddings(),
        llm=StructuredFakeCodexLLM(),
    )
    engine.index_documents([document()])

    result = engine.ask("¿Qué cambió para BESS?", k=3)

    assert result["mode"] == "local_vector+llm"
    assert result["error"] is None
    assert result["answer"] == (
        f"- La norma recuperada aborda almacenamiento. [{SOURCE} | {URL}]"
    )


def test_search_deduplicates_urls_and_preserves_explicit_source_diversity(
    tmp_path: Any,
) -> None:
    client = FakeClient()
    engine = VectorEngine(settings(tmp_path), client=client)
    cne = document("Subestaciones digitales para la red futura. " * 120)
    sea = {
        **document("Evaluación ambiental de proyectos en Coquimbo."),
        "title": "Evaluación de proyectos en Coquimbo",
        "url": "https://www.sea.gob.cl/noticias/proyectos-coquimbo",
        "source": "Servicio de Evaluación Ambiental (SEA)",
        "topics": ["evaluación ambiental"],
    }
    engine.index_documents([cne, sea])

    result = engine.search("¿Qué dicen CNE y SEA sobre proyectos?", k=2)

    assert {item["source"] for item in result["results"]} == {
        SOURCE,
        "Servicio de Evaluación Ambiental (SEA)",
    }
    assert len({item["url"] for item in result["results"]}) == 2


def test_query_failure_uses_lexical_extractive_fallback(tmp_path: Any) -> None:
    engine = VectorEngine(
        settings(tmp_path),
        client=FakeClient(),
        embeddings=FakeEmbeddings(),
        llm=BrokenLLM(),
    )
    engine.index_documents([document()])
    engine._embeddings = BrokenQueryEmbeddings()

    result = engine.ask("almacenamiento BESS", k=2)

    assert result["mode"] == "lexical_fallback+extractive"
    assert result["sources"][0]["url"] == URL
    assert f"[{SOURCE} | {URL}]" in result["answer"]
    assert "sin servicio de embeddings" in str(result["error"])
