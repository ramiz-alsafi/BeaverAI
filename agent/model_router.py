from __future__ import annotations

import logging
import os
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger("beaver")


# ── Provider patterns (checked in order) ──────────────────────────────────────

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^gpt-|^o1-|^o3-|^o4-|^text-davinci", re.I), "openai"),
    (re.compile(r"^claude-",                            re.I), "anthropic"),
    (re.compile(r"^gemini-|^palm-",                     re.I), "google"),
    (re.compile(r"^llama3?-|^mixtral-|^whisper-large",  re.I), "groq"),
    (re.compile(r"^mistral-|^codestral-|^open-mistral", re.I), "mistral"),
]


def _detect_provider(model: str) -> str:
    """Infer provider from model name. Returns 'ollama' if nothing matches."""
    for pattern, provider in _PATTERNS:
        if pattern.match(model):
            return provider
    return "ollama"


# ── LLM builders ──────────────────────────────────────────────────────────────

_OLLAMA_DEFAULT_URL = "http://localhost:11434"


def _build_ollama(
    model: str,
    temperature: float,
    base_url: str,
    num_ctx: int,
    num_gpu: int | None = None,      # FIX-6
    num_thread: int | None = None,   # FIX-6
    num_batch: int | None = None,    # FIX-6
    keep_alive: str | None = None,   # FIX-6
    low_vram: bool = False,          # FIX-6
    **_,
) -> BaseChatModel:
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        raise ImportError("Run: pip install langchain-ollama")

    _url = (base_url or "").strip() or _OLLAMA_DEFAULT_URL

    kwargs: dict[str, Any] = dict(
        model=model,
        temperature=temperature,
        base_url=_url,
        num_ctx=num_ctx,
    )
    # FIX-6: only pass num_gpu when explicitly set (>=0). -1 means "leave it
    # to Ollama's own VRAM estimate" — forcing a value on a small card can
    # OOM instead of gracefully falling back to CPU.
    if num_gpu is not None and num_gpu >= 0:
        kwargs["num_gpu"] = num_gpu
    if num_thread is not None:
        kwargs["num_thread"] = num_thread
    if num_batch is not None:
        kwargs["num_batch"] = num_batch
    if keep_alive is not None:
        kwargs["keep_alive"] = keep_alive
    if low_vram:
        kwargs["low_vram"] = low_vram

    return ChatOllama(**kwargs)


def _build_openai(model: str, temperature: float, base_url: str | None = None, **_) -> BaseChatModel:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError("Run: pip install langchain-openai")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY is not set in .env")
    kwargs: dict[str, Any] = dict(model=model, temperature=temperature, api_key=api_key)
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _build_anthropic(
    model: str,
    temperature: float,
    max_tokens: int | None = None,  # FIX-5
    **_,
) -> BaseChatModel:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise ImportError("Run: pip install langchain-anthropic")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in .env")
    # FIX-5: explicit max_tokens prevents truncation of long agentic responses.
    # Claude requires this field; langchain-anthropic's default is often too low.
    _max_tokens = max_tokens or int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))
    return ChatAnthropic(
        model=model,
        temperature=temperature,
        api_key=api_key,
        max_tokens=_max_tokens,
    )


def _build_google(model: str, temperature: float, **_) -> BaseChatModel:
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError("Run: pip install langchain-google-genai")
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GOOGLE_API_KEY is not set in .env")
    return ChatGoogleGenerativeAI(model=model, temperature=temperature, google_api_key=api_key)


def _build_groq(model: str, temperature: float, **_) -> BaseChatModel:
    try:
        from langchain_groq import ChatGroq
    except ImportError:
        raise ImportError("Run: pip install langchain-groq")
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY is not set in .env")
    return ChatGroq(model=model, temperature=temperature, groq_api_key=api_key)


def _build_mistral(model: str, temperature: float, **_) -> BaseChatModel:
    try:
        from langchain_mistralai import ChatMistralAI
    except ImportError:
        raise ImportError("Run: pip install langchain-mistralai")
    api_key = os.getenv("MISTRAL_API_KEY", "")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY is not set in .env")
    return ChatMistralAI(model=model, temperature=temperature, mistral_api_key=api_key)


def _build_openai_compat(model: str, temperature: float, base_url: str, **_) -> BaseChatModel:
    """Any OpenAI-compatible server (LM Studio, vLLM, llama.cpp, etc.)."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        raise ImportError("Run: pip install langchain-openai")
    # FIX-2: prefer a dedicated compat key so a real OPENAI_API_KEY is never
    # sent in request headers to a third-party compat server.
    api_key = (
        os.getenv("OPENAI_COMPAT_API_KEY")     # dedicated compat key (preferred)
        or os.getenv("OPENAI_API_KEY")          # shared key as last resort
        or "none"                               # most compat servers accept any value
    )
    return ChatOpenAI(model=model, temperature=temperature, base_url=base_url, api_key=api_key)


_BUILDERS = {
    "ollama":        _build_ollama,
    "local":         _build_ollama,    # alias — MODEL_PROVIDER=local works too
    "openai":        _build_openai,
    "anthropic":     _build_anthropic,
    "google":        _build_google,
    "groq":          _build_groq,
    "mistral":       _build_mistral,
    "openai_compat": _build_openai_compat,
}


# ── Public API ─────────────────────────────────────────────────────────────────

def get_llm(
    model: str | None = None,
    provider: str | None = None,
    temperature: float | None = None,
    base_url: str | None = None,
    num_ctx: int | None = None,
    num_gpu: int | None = None,      # FIX-6
    num_thread: int | None = None,   # FIX-6
    num_batch: int | None = None,    # FIX-6
    keep_alive: str | None = None,   # FIX-6
    low_vram: bool | None = None,    # FIX-6
) -> BaseChatModel:
    """Return a ready-to-use LangChain chat model.

    All parameters fall back to runtime_config when omitted, so the
    simplest call is just ``get_llm()`` anywhere in the graph.

    Parameters
    ----------
    model       : model name (e.g. "claude-sonnet-4-6", "gpt-4o", "qwen2.5:7b")
    provider    : explicit override — skips auto-detection
    temperature : sampling temperature (0.0 = deterministic)
    base_url    : required for ollama / openai_compat; ignored by cloud providers
    num_ctx     : context window tokens — ollama only
    num_gpu     : layers to force onto GPU — ollama only (FIX-6)
    num_thread  : CPU threads for non-offloaded layers — ollama only (FIX-6)
    num_batch   : prompt batch size — ollama only (FIX-6)
    keep_alive  : how long the model stays resident after a call — ollama only (FIX-6)
    low_vram    : enable Ollama's conservative low-VRAM mode — ollama only (FIX-6)
    """
    from agent.config import runtime_config  # late import avoids circular at module load

    _model       = model       or runtime_config.model
    _temperature = temperature if temperature is not None else runtime_config.temperature
    _base_url    = base_url    if base_url    is not None else runtime_config.base_url
    _num_ctx     = num_ctx     if num_ctx     is not None else runtime_config.num_ctx  # FIX-1
    # FIX-3: runtime_config.provider is a real attribute — no getattr guard needed
    _provider    = provider or runtime_config.provider or _detect_provider(_model)

    # FIX-6: forward GPU-offload tuning knobs from runtime_config when not
    # explicitly overridden by the caller.
    _num_gpu     = num_gpu     if num_gpu     is not None else runtime_config.num_gpu
    _num_thread  = num_thread  if num_thread  is not None else runtime_config.num_thread
    _num_batch   = num_batch   if num_batch   is not None else runtime_config.num_batch
    _keep_alive  = keep_alive  if keep_alive  is not None else runtime_config.keep_alive
    _low_vram    = low_vram    if low_vram    is not None else runtime_config.low_vram

    if _provider not in _BUILDERS:
        raise ValueError(
            f"Unknown provider '{_provider}'.\n"
            f"Supported: {list(_BUILDERS.keys())}"
        )

    logger.info("[MODEL ROUTER] provider=%s model=%s", _provider, _model)  # FIX-4

    return _BUILDERS[_provider](
        model=_model,
        temperature=_temperature,
        base_url=_base_url,
        num_ctx=_num_ctx,
        num_gpu=_num_gpu,
        num_thread=_num_thread,
        num_batch=_num_batch,
        keep_alive=_keep_alive,
        low_vram=_low_vram,
    )