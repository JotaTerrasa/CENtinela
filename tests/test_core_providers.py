from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.providers import (
    GenerationClient,
    OpenAICompatibleChatClient,
    OpenAICompatibleEmbeddings,
    OpenAIResponsesClient,
    ProviderExecutionError,
    ProviderOutputError,
    create_embeddings_client,
    create_generation_client,
)


class Recorder:
    def __init__(self) -> None:
        self.starts: list[dict[str, Any]] = []
        self.ends: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        self.starts.append({"serialized": serialized, "prompts": prompts, **kwargs})

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.ends.append({"response": response, **kwargs})

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.errors.append(error)


class Endpoint:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class ModelsEndpoint:
    def __init__(self, models: list[str] | None = None, error: BaseException | None = None):
        self.models = models or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(id=model) for model in self.models])


def responses_client(response: Any, *, models: list[str] | None = None) -> tuple[Any, Endpoint]:
    endpoint = Endpoint(response)
    client = SimpleNamespace(
        responses=endpoint,
        models=ModelsEndpoint(models),
    )
    return client, endpoint


def chat_client(response: Any, *, models: list[str] | None = None) -> tuple[Any, Endpoint]:
    endpoint = Endpoint(response)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint),
        models=ModelsEndpoint(models),
    )
    return client, endpoint


def test_openai_responses_structured_output_usage_and_callbacks() -> None:
    response = SimpleNamespace(
        id="resp_123",
        model="gpt-test-2026-08-13",
        output_text='{"approved":true}',
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=30,
            input_tokens_details=SimpleNamespace(cached_tokens=20),
            output_tokens_details=SimpleNamespace(reasoning_tokens=7),
        ),
    )
    sdk, endpoint = responses_client(response)
    callback = Recorder()
    ticks = iter((10.0, 10.4))
    llm = OpenAIResponsesClient(
        model="gpt-test",
        reasoning_effort="high",
        client=sdk,
        clock=lambda: next(ticks),
    )
    schema = {
        "type": "object",
        "properties": {"approved": {"type": "boolean"}},
        "required": ["approved"],
        "additionalProperties": False,
    }

    result = llm.invoke_json(
        "Evalua el informe",
        schema,
        config={"callbacks": [callback], "metadata": {"step": "judge"}},
    )

    assert isinstance(llm, GenerationClient)
    assert result.text == result.content == '{"approved":true}'
    assert result.data == result.structured_output == {"approved": True}
    assert result.response_id == result.thread_id == "resp_123"
    assert result.model == "gpt-test-2026-08-13"
    assert result.latency_seconds == pytest.approx(0.4)
    assert result.cost_usd is result.cost_clp is None
    assert result.usage is not None
    assert result.usage.to_dict() == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "cached_prompt_tokens": 20,
        "reasoning_tokens": 7,
        "total_tokens": 150,
    }
    request = endpoint.calls[0]
    assert request["model"] == "gpt-test"
    assert request["input"] == "Evalua el informe"
    assert request["reasoning"] == {"effort": "high"}
    assert request["store"] is False
    assert request["text"]["format"] == {
        "type": "json_schema",
        "name": "centinela_output",
        "strict": True,
        "schema": schema,
    }
    assert callback.starts[0]["invocation_params"]["model"] == "gpt-test"
    assert callback.starts[0]["metadata"] == {
        "step": "judge",
        "provider": "openai_api",
        "billing_mode": "api",
        "api_surface": "responses",
        "auth_method": "api_key",
        "cost_attribution": "token_pricing",
        "cost_status": "calculated_from_usage",
        "requested_model": "gpt-test",
    }
    usage = callback.ends[0]["response"].llm_output["token_usage"]
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    assert callback.errors == []


def test_openai_responses_extracts_text_from_output_blocks() -> None:
    response = {
        "id": "resp_blocks",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Primera linea"},
                    {"type": "output_text", "text": "Segunda linea"},
                ],
            }
        ],
    }
    sdk, _ = responses_client(response)

    result = OpenAIResponsesClient(model="gpt-test", client=sdk).invoke("Pregunta")

    assert result.text == "Primera linea\nSegunda linea"
    assert result.usage is None


def test_ollama_chat_uses_json_schema_and_self_hosted_accounting() -> None:
    response = SimpleNamespace(
        id="chatcmpl_local",
        model="qwen3.5:9b",
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer":"ok"}'))],
        usage=SimpleNamespace(
            prompt_tokens=40,
            completion_tokens=12,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        ),
    )
    sdk, endpoint = chat_client(response)
    callback = Recorder()
    llm = OpenAICompatibleChatClient(
        provider="ollama",
        model="qwen3.5:9b",
        reasoning_effort="low",
        base_url="http://ollama:11434/v1/",
        client=sdk,
    )
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    result = llm.invoke_json(
        "Responde",
        schema,
        config={"callbacks": callback},
    )

    request = endpoint.calls[0]
    assert request["messages"] == [{"role": "user", "content": "Responde"}]
    assert request["reasoning_effort"] == "low"
    assert request["response_format"]["json_schema"]["schema"] == schema
    assert result.data == {"answer": "ok"}
    assert result.usage is not None
    assert result.usage.prompt_tokens == 40
    assert result.usage.completion_tokens == 12
    assert result.usage.cached_prompt_tokens == 3
    assert result.usage.reasoning_tokens == 2
    start = callback.starts[0]
    assert start["invocation_params"]["model"] == "self-hosted/ollama/qwen3.5:9b"
    assert start["metadata"]["provider"] == "ollama"
    assert start["metadata"]["billing_mode"] == "self_hosted"
    assert start["metadata"]["endpoint"] == "http://ollama:11434/v1"


def test_invalid_structured_output_is_rejected_and_emits_callback_error() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="no es JSON"))]
    )
    sdk, _ = chat_client(response)
    callback = Recorder()
    llm = OpenAICompatibleChatClient(model="modelo", client=sdk)

    with pytest.raises(ProviderOutputError, match="JSON valido"):
        llm.invoke_json(
            "Pregunta",
            {"type": "object"},
            config={"callbacks": [callback]},
        )

    assert len(callback.errors) == 1
    assert callback.ends == []


def test_sdk_errors_are_wrapped_and_secrets_are_redacted() -> None:
    endpoint = Endpoint(
        error=RuntimeError(
            "Authorization: Bearer sk-super-secret prompt='contenido privado'"
        )
    )
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint),
        models=ModelsEndpoint(),
    )
    llm = OpenAICompatibleChatClient(model="modelo", client=sdk)

    with pytest.raises(ProviderExecutionError) as raised:
        llm.invoke("contenido privado")

    message = str(raised.value)
    assert "super-secret" not in message
    assert "contenido privado" not in message
    assert "[REDACTED" in message


def test_gateway_key_is_redacted_even_when_error_returns_the_raw_value() -> None:
    endpoint = Endpoint(error=RuntimeError("gateway-private"))
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint),
        models=ModelsEndpoint(),
    )
    llm = OpenAICompatibleChatClient(
        provider="vllm",
        model="modelo",
        api_key="gateway-private",
        client=sdk,
    )

    with pytest.raises(ProviderExecutionError) as raised:
        llm.invoke("pregunta")

    assert "gateway-private" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_gateway_cannot_echo_a_plain_prompt_into_an_error() -> None:
    private_prompt = "contrato confidencial ACME"
    endpoint = Endpoint(error=RuntimeError(f"La entrada rechazada fue: {private_prompt}"))
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint),
        models=ModelsEndpoint(),
    )
    llm = OpenAICompatibleChatClient(model="modelo", client=sdk)

    with pytest.raises(ProviderExecutionError) as raised:
        llm.invoke(private_prompt)

    assert private_prompt not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_embedding_endpoint_cannot_echo_plain_document_text() -> None:
    private_text = "documento interno no persistir"
    endpoint = Endpoint(error=RuntimeError(f"invalid input: {private_text}"))
    sdk = SimpleNamespace(embeddings=endpoint, models=ModelsEndpoint())
    embeddings = OpenAICompatibleEmbeddings(
        model="embed-model",
        client=sdk,
    )

    with pytest.raises(ProviderExecutionError) as raised:
        embeddings.embed_documents([private_text])

    assert private_text not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_gateway_cannot_echo_a_partial_prompt_into_an_error() -> None:
    private_prompt = "contrato confidencial ACME"
    endpoint = Endpoint(error=RuntimeError("Context overflow near: contrato confidencial"))
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint),
        models=ModelsEndpoint(),
    )
    llm = OpenAICompatibleChatClient(model="modelo", client=sdk)

    with pytest.raises(ProviderExecutionError) as raised:
        llm.invoke(private_prompt)

    message = str(raised.value)
    assert "contrato confidencial" not in message
    assert "RuntimeError" in message
    assert "[REDACTED]" in message


def test_health_distinguishes_endpoint_from_missing_model() -> None:
    sdk, _ = chat_client(response=None, models=["qwen3.5:4b", "qwen3.5:9b"])
    ticks = iter((20.0, 20.25))
    llm = OpenAICompatibleChatClient(
        model="qwen3.5:27b",
        client=sdk,
        clock=lambda: next(ticks),
    )

    health = llm.health(timeout_seconds=3)

    assert health.reachable is True
    assert health.available is False
    assert health.model_available is False
    assert health.models == ("qwen3.5:4b", "qwen3.5:9b")
    assert health.latency_seconds == pytest.approx(0.25)
    assert "no esta cargado" in health.detail
    assert health.to_dict()["models"] == ["qwen3.5:4b", "qwen3.5:9b"]


def test_health_failure_does_not_expose_sdk_error() -> None:
    sdk = SimpleNamespace(models=ModelsEndpoint(error=RuntimeError("token=secreto")))
    llm = OpenAIResponsesClient(model="gpt-test", client=sdk)

    health = llm.health()

    assert health.available is health.reachable is False
    assert health.authenticated is False
    assert "secreto" not in health.detail
    assert health.detail.endswith("RuntimeError")


def test_embeddings_batch_preserves_provider_indices_and_validates_dimension() -> None:
    first = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
        ]
    )
    second = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.5, 0.5])])
    endpoint = Endpoint()
    responses = iter((first, second))

    def create(**kwargs: Any) -> Any:
        endpoint.calls.append(kwargs)
        return next(responses)

    endpoint.create = create  # type: ignore[method-assign]
    sdk = SimpleNamespace(embeddings=endpoint, models=ModelsEndpoint(["qwen-embed"]))
    embeddings = OpenAICompatibleEmbeddings(
        model="qwen-embed",
        provider="ollama",
        batch_size=2,
        dimensions=2,
        client=sdk,
    )

    vectors = embeddings.embed_documents(["uno", "dos", "tres"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    assert [call["input"] for call in endpoint.calls] == [["uno", "dos"], ["tres"]]
    assert all(call["dimensions"] == 2 for call in endpoint.calls)
    assert embeddings.model_name == embeddings.model == "qwen-embed"


def test_openai_embedding_usage_is_emitted_to_langchain_callback() -> None:
    response = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])],
        usage=SimpleNamespace(prompt_tokens=11, total_tokens=11),
    )
    endpoint = Endpoint(response)
    sdk = SimpleNamespace(
        embeddings=endpoint,
        models=ModelsEndpoint(["text-embedding-3-small"]),
    )
    callback = Recorder()
    embeddings = OpenAICompatibleEmbeddings(
        model="text-embedding-3-small",
        provider="openai_api",
        api_key="secret",
        client=sdk,
        callback=callback,
    )

    assert embeddings.embed_query("texto público") == [1.0, 0.0]
    assert callback.starts[0]["metadata"]["api_surface"] == "embeddings"
    assert callback.starts[0]["metadata"]["billing_mode"] == "api"
    usage = callback.ends[0]["response"].llm_output["token_usage"]
    assert usage == {
        "prompt_tokens": 11,
        "completion_tokens": 0,
        "total_tokens": 11,
    }


def test_embeddings_empty_input_and_invalid_vectors() -> None:
    endpoint = Endpoint(SimpleNamespace(data=[SimpleNamespace(embedding=[float("nan")])]))
    sdk = SimpleNamespace(embeddings=endpoint, models=ModelsEndpoint())
    embeddings = OpenAICompatibleEmbeddings(model="embed", client=sdk)

    assert embeddings.embed_documents([]) == []
    with pytest.raises(ValueError, match="no vacio"):
        embeddings.embed_query(" ")
    with pytest.raises(ProviderOutputError, match="no finito"):
        embeddings.embed_query("pregunta")


def test_factories_route_http_providers_without_importing_sdk() -> None:
    sdk, _ = chat_client(response=None)
    ollama = create_generation_client(
        "ollama",
        model="qwen",
        client=sdk,
    )
    openai_sdk, _ = responses_client(response=None)
    openai = create_generation_client(
        "openai",
        model="gpt",
        client=openai_sdk,
    )
    embeddings = create_embeddings_client(
        "vllm",
        model="embed",
        client=SimpleNamespace(embeddings=Endpoint(), models=ModelsEndpoint()),
    )

    assert isinstance(ollama, OpenAICompatibleChatClient)
    assert ollama.base_url == "http://localhost:11434/v1"
    assert isinstance(openai, OpenAIResponsesClient)
    assert isinstance(embeddings, OpenAICompatibleEmbeddings)
    assert embeddings.base_url == "http://localhost:8000/v1"


@pytest.mark.parametrize(
    "url",
    [
        "ollama:11434/v1",
        "ftp://ollama/v1",
        "http://user:secret@ollama/v1",
        "http://ollama/v1?api_key=secret",
    ],
)
def test_base_url_rejects_unsafe_or_ambiguous_values(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleChatClient(model="modelo", base_url=url, client=object())
