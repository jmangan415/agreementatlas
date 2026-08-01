from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_EXTRACTOR_MODEL = "google/gemma-4-26b-a4b-qat"
DEFAULT_EMBEDDING_MODEL = "text-embedding-nomic-embed-text-v1.5"
DEFAULT_NO_THINK_MODELS = "qwen3.6-27b-mlx"


class LMStudioError(RuntimeError):
    pass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LMStudioClient:
    """Server-side LM Studio client with explicit loaded-instance management."""

    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1"
        ).rstrip("/")
    )
    token: str = field(default_factory=lambda: os.environ.get("LMSTUDIO_API_TOKEN", ""))
    timeout: int = field(
        default_factory=lambda: int(os.environ.get("LMSTUDIO_TIMEOUT_SECONDS", "180"))
    )
    managed_instance_ids: set[str] = field(default_factory=set)

    @property
    def server_url(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    @property
    def extractor_model(self) -> str:
        return os.environ.get("LMSTUDIO_MODEL", "").strip() or DEFAULT_EXTRACTOR_MODEL

    @property
    def embedding_model(self) -> str:
        return (
            os.environ.get("LMSTUDIO_EMBEDDING_MODEL", "").strip()
            or DEFAULT_EMBEDDING_MODEL
        )

    @property
    def automanage(self) -> bool:
        return env_bool("LMSTUDIO_AUTOMANAGE_MODELS", False)

    def allowed_model_ids(self) -> set[str]:
        configured = {
            item.strip()
            for item in os.environ.get("LMSTUDIO_ALLOWED_MODELS", "").split(",")
            if item.strip()
        }
        configured.update({self.extractor_model, self.embedding_model})
        return configured

    def no_think_model_ids(self) -> set[str]:
        configured = os.environ.get("LMSTUDIO_NO_THINK_MODELS", DEFAULT_NO_THINK_MODELS)
        return {item.strip() for item in configured.split(",") if item.strip()}

    def model_reference_allowed(self, model: str) -> bool:
        allowed = self.allowed_model_ids()
        if model in allowed:
            return True
        try:
            return any(
                model in {item["id"], item["key"]}
                and bool({item["id"], item["key"]} & allowed)
                for item in self.loaded_models()
            )
        except LMStudioError:
            return False

    def prepare_user_message(self, model: str, user: str) -> str:
        """Apply model-specific chat-template directives without affecting others."""

        no_think = self.no_think_model_ids()
        enabled = model in no_think
        if not enabled:
            try:
                enabled = any(
                    model == item["id"] and item["key"] in no_think
                    for item in self.loaded_models()
                )
            except LMStudioError:
                enabled = False
        if enabled:
            return f"{user.rstrip()}\n\n/no_think"
        return user

    def _request_url(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self.timeout
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
                if not isinstance(value, dict):
                    raise LMStudioError("LM Studio returned the wrong JSON shape.")
                return value
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LMStudioError(
                f"LM Studio returned HTTP {exc.code}: {detail[:1000]}"
            ) from exc
        except LMStudioError:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LMStudioError(f"Could not reach LM Studio at {url}: {exc}") from exc

    def _openai_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._request_url(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            payload,
            timeout,
        )

    def _native_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        return self._request_url(
            method,
            f"{self.server_url}/api/v1/{path.lstrip('/')}",
            payload,
            timeout,
        )

    def native_models(self) -> list[dict[str, Any]]:
        value = self._native_request("GET", "models", timeout=4)
        models = value.get("models", [])
        return list(models) if isinstance(models, list) else []

    def downloaded_models(self) -> list[dict[str, Any]]:
        return self.native_models()

    def loaded_models(
        self, models: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Loaded instances, from a model list already in hand where there is one.

        Every caller here used to re-fetch. A single status() cost four
        identical GETs to /api/v1/models, and the interface polls status every
        fifteen seconds.
        """

        loaded: list[dict[str, Any]] = []
        for model in self.native_models() if models is None else models:
            instances = model.get("loaded_instances", [])
            if not isinstance(instances, list):
                continue
            for instance in instances:
                if not isinstance(instance, dict):
                    continue
                loaded.append(
                    {
                        "id": str(instance.get("id", "")),
                        "key": str(model.get("key", "")),
                        "type": str(model.get("type", "")),
                        "display_name": str(model.get("display_name", "")),
                        "config": instance.get("config", {}),
                        "managed_by_agreementatlas": (
                            str(instance.get("id", "")) in self.managed_instance_ids
                        ),
                    }
                )
        return loaded

    def models(self) -> list[dict[str, Any]]:
        """Compatibility method: return genuinely loaded inference instances."""

        return [
            {
                "id": item["id"],
                "owned_by": "local",
                "type": item["type"],
                "key": item["key"],
                "config": item["config"],
            }
            for item in self.loaded_models()
        ]

    def model_id(self, requested: str = "") -> str:
        model = requested or self.extractor_model
        allowed = self.allowed_model_ids()
        loaded = [item for item in self.loaded_models() if item.get("type") == "llm"]
        match = next(
            (
                item
                for item in loaded
                if model in {item["id"], item["key"]}
                and bool({item["id"], item["key"]} & allowed)
            ),
            None,
        )
        if not match and model not in allowed:
            raise LMStudioError("The requested model is not in the server allowlist.")
        if not match:
            raise LMStudioError(f"Configured extractor model is not loaded: {model}")
        return str(match["id"])

    def load_model(
        self,
        model: str,
        *,
        context_length: int | None = None,
    ) -> dict[str, Any]:
        if not self.model_reference_allowed(model):
            raise LMStudioError("The requested model is not in the server allowlist.")
        payload: dict[str, Any] = {"model": model, "echo_load_config": True}
        if context_length:
            payload["context_length"] = context_length
        result = self._native_request("POST", "models/load", payload)
        instance_id = str(result.get("instance_id", ""))
        if not instance_id:
            raise LMStudioError(
                "LM Studio loaded a model without an instance identifier."
            )
        self.managed_instance_ids.add(instance_id)
        return result

    def unload_model(self, instance_id: str) -> dict[str, Any]:
        if instance_id not in self.managed_instance_ids:
            raise LMStudioError(
                "AgreementAtlas will only unload model instances it loaded itself."
            )
        result = self._native_request(
            "POST", "models/unload", {"instance_id": instance_id}
        )
        self.managed_instance_ids.discard(instance_id)
        return result

    def ensure_configured_models(
        self, models: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        loaded = self.loaded_models(models)
        loaded_by_key = {item["key"]: item for item in loaded}
        loaded_by_id = {item["id"]: item for item in loaded}
        wanted = (
            (self.extractor_model, "llm", 32768),
            (self.embedding_model, "embedding", 2048),
        )
        started_anything = False
        for model, _, context in wanted:
            if model in loaded_by_key or model in loaded_by_id:
                continue
            if self.automanage:
                self.load_model(model, context_length=context)
                started_anything = True
        # Re-reading only tells us something new when something was loaded.
        refreshed = self.loaded_models() if started_anything else loaded
        return {
            "automanage": self.automanage,
            "extractor": next(
                (
                    item
                    for item in refreshed
                    if item["id"] == self.extractor_model
                    or item["key"] == self.extractor_model
                ),
                None,
            ),
            "embedder": next(
                (
                    item
                    for item in refreshed
                    if item["id"] == self.embedding_model
                    or item["key"] == self.embedding_model
                ),
                None,
            ),
        }

    def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.1,
        max_tokens: int = 1800,
        schema: dict[str, Any] | None = None,
    ) -> str:
        if model not in self.allowed_model_ids():
            raise LMStudioError("The requested model is not in the server allowlist.")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": self.prepare_user_message(model, user),
                },
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "seed": 42,
            "reasoning_effort": "none",
        }
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agreementatlas_legal_extraction",
                    "strict": True,
                    "schema": schema,
                },
            }
        result = self._openai_request("POST", "chat/completions", payload)
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LMStudioError("LM Studio returned an unexpected response.") from exc
        if not content:
            raise LMStudioError(
                "The selected model returned empty content; disable hidden reasoning."
            )
        return str(content)

    def chat_stream(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.1,
        max_tokens: int = 1800,
        reasoning: bool = False,
    ):
        """Yield the answer in pieces as the model writes it.

        `chat` waits for the whole completion, which on a local model is tens
        of seconds of blank screen and then a wall of text arriving at once.
        The extraction and benchmark paths keep using `chat`: they parse whole
        JSON documents and have nothing to show a reader mid-flight.
        """

        if model not in self.allowed_model_ids():
            raise LMStudioError("The requested model is not in the server allowlist.")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": self.prepare_user_message(model, user)},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "seed": 42,
        }
        # Reasoning arrives in a separate reasoning_content delta that the
        # non-streaming path never sees -- the earlier experiment read only
        # "content" and concluded the model had answered nothing. When it is
        # off we say so explicitly, because the model defaults to thinking.
        if reasoning:
            # A deliberating model can cycle -- re-checking the same three
            # facts until the budget dies. The window is 64k; the budget is
            # generous so exhaustion is the exception, and the caller treats
            # an empty answer as "stop thinking and answer" rather than fail.
            payload["max_tokens"] = max(max_tokens, 12000)
        else:
            payload["reasoning_effort"] = "none"
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    try:
                        delta = chunk["choices"][0]["delta"]
                    except (KeyError, IndexError, TypeError):
                        continue
                    thinking = delta.get("reasoning_content") or ""
                    if thinking:
                        yield ("thinking", thinking)
                    piece = delta.get("content") or ""
                    if piece:
                        yield ("token", piece)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LMStudioError(
                f"LM Studio returned HTTP {exc.code}: {detail[:1000]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LMStudioError(f"Could not reach LM Studio at {url}: {exc}") from exc

    def structured_chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 3000,
    ) -> dict[str, Any]:
        content = self.chat(
            model=model,
            system=system,
            user=user,
            temperature=0,
            max_tokens=max_tokens,
            schema=schema,
        )
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LMStudioError(
                "The selected model did not return valid structured JSON."
            ) from exc
        if not isinstance(value, dict):
            raise LMStudioError("The selected model returned the wrong JSON shape.")
        return value

    def embeddings(
        self,
        texts: Iterable[str],
        *,
        model: str | None = None,
        input_type: str,
    ) -> list[list[float]]:
        selected = model or self.embedding_model
        if selected not in self.allowed_model_ids():
            raise LMStudioError("The embedding model is not in the server allowlist.")
        if input_type not in {"search_document", "search_query"}:
            raise LMStudioError(
                "Embedding input type must be search_document or search_query."
            )
        prepared = [f"{input_type}: {normalise_embedding_text(text)}" for text in texts]
        if not prepared:
            return []
        result = self._openai_request(
            "POST", "embeddings", {"model": selected, "input": prepared}
        )
        data = result.get("data", [])
        if not isinstance(data, list) or len(data) != len(prepared):
            raise LMStudioError(
                "LM Studio returned the wrong embedding response shape."
            )
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding", [])
            if not isinstance(vector, list) or not vector:
                raise LMStudioError("LM Studio returned an empty embedding.")
            vectors.append([float(value) for value in vector])
        return vectors

    def status(self) -> dict[str, Any]:
        try:
            downloaded = self.native_models()
            loaded = self.loaded_models(downloaded)
            configured = self.ensure_configured_models(downloaded)
            return {
                "available": True,
                "base_url": self.server_url,
                "downloaded_count": len(downloaded),
                "models": [
                    {
                        "id": item["id"],
                        "key": item["key"],
                        "type": item["type"],
                        "display_name": item["display_name"],
                        "config": item["config"],
                        "managed_by_agreementatlas": item["managed_by_agreementatlas"],
                    }
                    for item in loaded
                ],
                "extractor": configured["extractor"],
                "embedder": configured["embedder"],
                "automanage": configured["automanage"],
            }
        except LMStudioError as exc:
            return {
                "available": False,
                "base_url": self.server_url,
                "downloaded_count": 0,
                "models": [],
                "extractor": None,
                "embedder": None,
                "automanage": self.automanage,
                "error": str(exc),
            }


def normalise_embedding_text(value: str) -> str:
    return " ".join(value.replace("\n", " ").split())
