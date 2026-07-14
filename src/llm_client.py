"""Cloud-only LLM client.

Waterfall order:
  1. NVIDIA NIM API (primary model for the task)
  2. NVIDIA NIM API (fallback model)
  3. Groq
  4. Ollama Cloud (cloud-hosted model via the local Ollama daemon;
     model name must end with "-cloud" so local models are never used)

Raises LLMError when every provider fails, so callers can tell
"the LLM said no matches" apart from "we never got an answer".
"""
import os
import time
import re
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import config
from logger import get_logger

logger = get_logger()
load_dotenv()

RETRIES_PER_MODEL = 2
RETRY_BACKOFF_SECONDS = 5


class LLMError(Exception):
    """All providers failed to produce a response."""


_nvidia_client = None
_groq_client = None
_ollama_client = None


def _get_nvidia_client():
    global _nvidia_client
    if _nvidia_client is None and os.getenv("NVIDIA_API_KEY"):
        _nvidia_client = OpenAI(
            base_url=config.NVIDIA_BASE_URL,
            api_key=os.getenv("NVIDIA_API_KEY"),
        )
    return _nvidia_client


def _get_groq_client():
    global _groq_client
    if _groq_client is None and os.getenv("GROQ_API_KEY"):
        _groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _groq_client


def _get_ollama_client():
    """Ollama Cloud only: requires a '-cloud' model name (proxied through the
    local Ollama daemon after `ollama signin`). Local models are never used."""
    global _ollama_client
    if _ollama_client is None and config.OLLAMA_CLOUD_MODEL.endswith("-cloud"):
        _ollama_client = OpenAI(
            base_url=config.OLLAMA_BASE_URL,
            api_key="ollama",  # auth handled by ollama signin
        )
    return _ollama_client


def extract_json_from_markdown(text):
    if "```json" in text:
        return text.split("```json")[1].split("```")[0].strip()
    if "```" in text:
        return text.split("```")[1].split("```")[0].strip()
    return text.strip()


def _strip_reasoning(text):
    """Remove <think>...</think> blocks emitted by reasoning models."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _call_model(client, model, prompt, is_json, temperature, max_tokens):
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
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
    result = _strip_reasoning(response.choices[0].message.content or "")
    if not result:
        raise ValueError("empty response")
    return extract_json_from_markdown(result) if is_json else result


def generate_content(prompt, is_json=False, temperature=0.3, max_tokens=config.MAX_OUTPUT_TOKENS, model=None):
    """Generate text via the cloud provider waterfall.

    Returns the response text. Raises LLMError if every provider fails.
    """
    attempts = []
    nvidia = _get_nvidia_client()
    if nvidia:
        primary = model or config.MATCH_MODEL
        attempts.append(("NVIDIA", nvidia, primary))
        if config.NVIDIA_FALLBACK_MODEL != primary:
            attempts.append(("NVIDIA", nvidia, config.NVIDIA_FALLBACK_MODEL))
    else:
        logger.warning("NVIDIA_API_KEY not set - skipping NVIDIA NIM (get a free key at https://build.nvidia.com)")

    groq = _get_groq_client()
    if groq:
        attempts.append(("Groq", groq, config.GROQ_MODEL))

    ollama = _get_ollama_client()
    if ollama:
        attempts.append(("Ollama Cloud", ollama, config.OLLAMA_CLOUD_MODEL))

    if not attempts:
        raise LLMError("No LLM providers configured. Set NVIDIA_API_KEY and/or GROQ_API_KEY in .env")

    last_error = None
    for provider, client, model_id in attempts:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                return _call_model(client, model_id, prompt, is_json, temperature, max_tokens)
            except Exception as e:
                last_error = e
                logger.warning(f"{provider} ({model_id}) attempt {attempt}/{RETRIES_PER_MODEL} failed: {e}")
                if attempt < RETRIES_PER_MODEL:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        logger.error(f"{provider} ({model_id}) exhausted, trying next provider/model...")

    raise LLMError(f"All LLM providers failed. Last error: {last_error}")
