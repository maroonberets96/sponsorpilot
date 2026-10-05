"""Cloud-only LLM client.

Every provider speaks the OpenAI chat-completions protocol, so one client class
(openai.OpenAI) drives all of them - only the base URL, key and model change.
Providers are tried in config.LLM_ORDER and the chain fails over on rate limits,
dead keys and errors, which free tiers produce constantly. Spare keys for the
same provider (NVIDIA_API_KEY_2, GROQ_API_KEY_2, ...) are registered as extra
fallback slots, so every key you have gets used before the chain gives up.

Default waterfall (keys permitting):
  Groq -> Cerebras -> Gemini -> Hugging Face -> NVIDIA NIM -> OpenRouter -> Ollama Cloud

Raises LLMError when every provider/key fails, so callers can tell
"the LLM said no matches" apart from "we never got an answer".
"""
import os
import time
import re
from openai import OpenAI
from dotenv import load_dotenv
import config
from logger import get_logger

logger = get_logger()
load_dotenv()

# One quick retry for genuinely transient faults (timeout, dropped connection,
# 5xx). Rate limits, dead keys and retired models do NOT retry the same slot -
# there is no point waiting for a quota to reset mid-run when the next provider
# is a fresh quota, so the chain fails straight over to it.
RETRIES_PER_MODEL = 2
RETRY_BACKOFF_SECONDS = 3


def _is_transient(exc):
    """True if retrying the SAME provider might help; False if we should fail
    over immediately (rate limit, auth, retired/unknown model, bad request)."""
    # Prefer an HTTP status code when the SDK exposes one.
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status is not None:
        # 408 request timeout and 5xx are worth one retry; everything else
        # (401/403 auth, 404/410 model gone, 422/400 bad request, 429 quota)
        # will not improve by asking the same slot again.
        return status == 408 or status >= 500
    # No status: connection resets / timeouts raised as plain exceptions.
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return any(w in name for w in ("timeout", "connection")) or \
        any(w in text for w in ("timed out", "connection", "temporarily"))


def _err_brief(exc):
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    msg = str(exc).split("\n", 1)[0][:120]
    return f"HTTP {status}: {msg}" if status else msg

# Groq, Cerebras and the Hugging Face router sit behind Cloudflare, which can
# answer a non-browser User-Agent with HTTP 403 "error code: 1010". A
# browser-shaped UA is all it wants.
_CLOUDFLARE_SAFE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JobAppAssistant/1.0"

# Provider registry. Each entry: how to reach the provider and any per-provider
# quirks. `needs_key=False` means the endpoint accepts an unauthenticated /
# placeholder key (the local Ollama daemon). `headers` are sent on every
# request; `extra_body` carries non-standard params (Gemini must be told to
# keep its reasoning budget low or it streams back empty content).
_PROVIDERS = {
    "groq": {
        "base_url": config.GROQ_BASE_URL,
        "key_env": "GROQ_API_KEY",
        "model": config.GROQ_MODEL,
        "headers": {"User-Agent": _CLOUDFLARE_SAFE_UA},
    },
    "cerebras": {
        "base_url": config.CEREBRAS_BASE_URL,
        "key_env": "CEREBRAS_API_KEY",
        "model": config.CEREBRAS_MODEL,
        "headers": {"User-Agent": _CLOUDFLARE_SAFE_UA},
    },
    "gemini": {
        "base_url": config.GEMINI_BASE_URL,
        "key_env": "GEMINI_API_KEY",
        "model": config.GEMINI_MODEL,
        "extra_body": {"reasoning_effort": "low"},
    },
    "huggingface": {
        "base_url": config.HUGGINGFACE_BASE_URL,
        "key_env": "HUGGINGFACE_API_KEY",
        "model": config.HUGGINGFACE_MODEL,
        "headers": {"User-Agent": _CLOUDFLARE_SAFE_UA},
    },
    "nvidia": {
        "base_url": config.NVIDIA_BASE_URL,
        "key_env": "NVIDIA_API_KEY",
        "model": config.NVIDIA_MODEL,
        "fallback_model": config.NVIDIA_FALLBACK_MODEL,
    },
    "openrouter": {
        "base_url": config.OPENROUTER_BASE_URL,
        "key_env": "OPENROUTER_API_KEY",
        "model": config.OPENROUTER_MODEL,
        "headers": {
            "HTTP-Referer": "https://github.com/local/job-application-assistant",
            "X-Title": "Job Application Assistant",
        },
    },
    "mistral": {
        "base_url": config.MISTRAL_BASE_URL,
        "key_env": "MISTRAL_API_KEY",
        "model": config.MISTRAL_MODEL,
    },
    "requesty": {
        "base_url": config.REQUESTY_BASE_URL,
        "key_env": "REQUESTY_API_KEY",
        "model": config.REQUESTY_MODEL,
    },
    "ollama": {
        "base_url": config.OLLAMA_BASE_URL,
        "key_env": "OLLAMA_API_KEY",
        "model": config.OLLAMA_CLOUD_MODEL,
        # Cloud endpoint needs OLLAMA_API_KEY; the local daemon accepts the
        # placeholder "ollama". Either way a key is not strictly required.
        "needs_key": False,
    },
}

# Suffixes scanned for spare keys of the same provider: GROQ_API_KEY,
# GROQ_API_KEY_2, GROQ_API_KEY_3, ...
_KEY_SUFFIXES = ("", "_2", "_3", "_4", "_5")


class LLMError(Exception):
    """All providers failed to produce a response."""


# Cache clients by (provider, key) so repeated calls reuse the connection pool.
_clients = {}


def _client(name, base_url, api_key, headers):
    cache_key = (name, api_key)
    if cache_key not in _clients:
        _clients[cache_key] = OpenAI(
            base_url=base_url,
            api_key=api_key or "ollama",  # Ollama local daemon ignores the key
            default_headers=headers or None,
        )
    return _clients[cache_key]


def _keys_for(cfg):
    """All keys for a provider: base key plus _2.._5 spares (in order)."""
    keys = [os.getenv(f"{cfg['key_env']}{s}", "").strip() for s in _KEY_SUFFIXES]
    keys = [k for k in keys if k]
    if not keys and not cfg.get("needs_key", True):
        keys = [""]  # e.g. local Ollama daemon, no key required
    return keys


def _extra_body_for(cfg, model):
    """Per-request extras, plus disabling Nemotron's chain-of-thought so the
    full token budget goes to the answer rather than hidden reasoning."""
    extra = dict(cfg.get("extra_body") or {})
    if "nemotron" in model:
        extra.setdefault("chat_template_kwargs", {"thinking": False})
    return extra or None


def _provider_attempts(name, model):
    """Attempts (one per key) for a single provider at a given model."""
    cfg = _PROVIDERS.get(name)
    if not cfg or not cfg["base_url"]:
        return []
    out = []
    for i, key in enumerate(_keys_for(cfg)):
        slot = name if i == 0 else f"{name}#{i + 1}"
        client = _client(slot, cfg["base_url"], key, cfg.get("headers"))
        out.append((slot, client, model, _extra_body_for(cfg, model)))
    return out


def _attempts(nvidia_model_override=None, prefer=None):
    """Build the ordered list of (label, client, model, extra_body) to try.

    `prefer` is a list of (provider, model) tried FIRST (quality-first writing).
    Then every provider in LLM_ORDER contributes one attempt per key found
    (base key plus _2.._5 spares); NVIDIA also contributes a fallback-model
    attempt per key. Duplicate (client, model) attempts are skipped, so a
    preferred model is not retried when the general chain reaches it again.
    """
    attempts, seen = [], set()

    def add(items):
        for label, client, model, extra in items:
            key = (id(client), model)
            if key in seen:
                continue
            seen.add(key)
            attempts.append((label, client, model, extra))

    for name, model in (prefer or []):
        add((f"prefer:{lbl}", c, m, e) for (lbl, c, m, e) in _provider_attempts(name, model))

    for name in config.LLM_ORDER:
        cfg = _PROVIDERS.get(name)
        if not cfg:
            continue
        model = cfg["model"]
        if name == "nvidia" and nvidia_model_override:
            model = nvidia_model_override
        add(_provider_attempts(name, model))
        fb = cfg.get("fallback_model")
        if fb and fb != model:
            add(_provider_attempts(name, fb))
    return attempts


def extract_json_from_markdown(text):
    if "```json" in text:
        return text.split("```json")[1].split("```")[0].strip()
    if "```" in text:
        return text.split("```")[1].split("```")[0].strip()
    return text.strip()


def _strip_reasoning(text):
    """Remove <think>...</think> blocks emitted by reasoning models."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_model(client, model, prompt, is_json, temperature, max_tokens, extra_body):
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    if is_json:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception:
        if not is_json:
            raise
        # Some models reject response_format; retry once without it
        kwargs.pop("response_format")
        response = client.chat.completions.create(**kwargs)
    message = response.choices[0].message
    # A model that ignored the "don't think out loud" hint may put the answer
    # in reasoning_content instead of content; salvage it rather than failing.
    content = message.content or getattr(message, "reasoning_content", "") or ""
    result = _strip_reasoning(content)
    if not result:
        raise ValueError("empty response")
    return extract_json_from_markdown(result) if is_json else result


def generate_content(prompt, is_json=False, temperature=0.3, max_tokens=config.MAX_OUTPUT_TOKENS, model=None, prefer=None):
    """Generate text via the cloud provider waterfall.

    `model`, when given, overrides the NVIDIA model only (so callers can ask
    NVIDIA for its match model); every other provider uses its own configured
    model. `prefer` is a list of (provider, model) tried FIRST - used by the
    writing path to lead with the strongest model before the general chain.
    Returns the response text. Raises LLMError if every provider/key fails.
    """
    attempts = _attempts(nvidia_model_override=model, prefer=prefer)
    if not attempts:
        raise LLMError(
            "No LLM providers configured. Set at least one of GROQ_API_KEY, "
            "CEREBRAS_API_KEY, GEMINI_API_KEY, HUGGINGFACE_API_KEY, "
            "NVIDIA_API_KEY, OPENROUTER_API_KEY or OLLAMA_API_KEY in .env"
        )

    last_error = None
    for label, client, model_id, extra_body in attempts:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                return _call_model(
                    client, model_id, prompt, is_json, temperature, max_tokens, extra_body
                )
            except Exception as e:
                last_error = e
                transient = _is_transient(e)
                if transient and attempt < RETRIES_PER_MODEL:
                    logger.warning(f"{label} ({model_id}) transient fault ({_err_brief(e)}); "
                                   f"retry {attempt}/{RETRIES_PER_MODEL - 1}")
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                # Non-transient (rate limit / auth / model gone), or out of
                # retries: stop wasting time here and fail over to the next slot.
                logger.warning(f"{label} ({model_id}) failed ({_err_brief(e)}); "
                               f"failing over to next provider.")
                break

    raise LLMError(f"All LLM providers failed. Last error: {last_error}")
