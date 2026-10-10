"""agent.model_router._detect_provider — name -> provider auto-detection.

[FIX-ROUTER-1] Ollama's own library ships tags that collide with cloud
name prefixes (gpt-oss:20b, llama3-gradient:8b ...). Ollama tags are always
"name:tag"; no cloud model id contains a colon.
"""
import pytest

from agent.model_router import _detect_provider


@pytest.mark.parametrize(
    "model",
    ["gpt-oss:20b", "gpt-oss:120b", "llama3-gradient:8b", "llama3-groq-tool-use:8b",
     "qwen2.5:7b", "mistral-nemo:12b", "claude-ish:latest"],
)
def test_colon_tags_are_always_local_ollama(model):
    assert _detect_provider(model) == "ollama"


@pytest.mark.parametrize(
    "model, provider",
    [
        ("gpt-4o", "openai"),
        ("gpt-4o-mini", "openai"),
        ("o3-mini", "openai"),
        ("claude-sonnet-4-6", "anthropic"),
        ("gemini-1.5-pro", "google"),
        ("llama3-70b-8192", "groq"),
        ("mixtral-8x7b-32768", "groq"),
        ("mistral-large-latest", "mistral"),
        ("codestral-latest", "mistral"),
    ],
)
def test_cloud_ids_still_route_to_their_provider(model, provider):
    assert _detect_provider(model) == provider


def test_unknown_names_fall_back_to_ollama():
    assert _detect_provider("some-random-local-model") == "ollama"
