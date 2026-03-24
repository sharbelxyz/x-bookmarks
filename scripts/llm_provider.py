#!/usr/bin/env python3
"""
LLM Provider abstraction — routes prompts to Ollama, Gemini, or Claude.

Configuration via .env:
    LLM_PROVIDER=ollama        # ollama | gemini | claude
    LLM_MODEL=qwen3:8b         # model name (provider-specific)
    LLM_BATCH_SIZE=20           # tweets per batch (lower for local models)
    GEMINI_API_KEY=...          # only needed if provider=gemini
    ANTHROPIC_API_KEY=...       # only needed if provider=claude
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.error


# ─── Configuration ────────────────────────────────────────────────────────────

def _load_env():
    """Load .env file if present (same logic as service.py)."""
    from pathlib import Path
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").lower()
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_BATCH_SIZE = int(os.environ.get("LLM_BATCH_SIZE", "20"))

# Ollama
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_PATH = "/opt/homebrew/bin/ollama"
_ollama_models_raw = os.environ.get("OLLAMA_MODELS", "")
OLLAMA_MODELS = [m.strip() for m in _ollama_models_raw.split(",") if m.strip()] or [LLM_MODEL]

# Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


# ─── Dispatcher ───────────────────────────────────────────────────────────────

def call_llm(prompt, max_tokens=8192, retries=3, model=None):
    """Dispatch to the configured LLM provider. Optional model overrides default."""
    if LLM_PROVIDER == "ollama":
        return call_ollama(prompt, model=model, max_tokens=max_tokens, retries=retries)
    elif LLM_PROVIDER == "gemini":
        return call_gemini(prompt, max_tokens=max_tokens, retries=retries)
    elif LLM_PROVIDER == "claude":
        return call_claude(prompt, max_tokens=max_tokens, retries=retries)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}. Use 'ollama', 'gemini', or 'claude'.")


def get_batch_size():
    """Return the configured batch size for the current provider."""
    return LLM_BATCH_SIZE


def get_ollama_models():
    """Return list of Ollama models for parallel processing."""
    return OLLAMA_MODELS


# ─── Ollama Provider ─────────────────────────────────────────────────────────

def _ensure_ollama_running():
    """Try to start Ollama if it's not running."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        urllib.request.urlopen(req, timeout=3)
        return True  # Already running
    except Exception:
        pass

    # Try to start it
    try:
        subprocess.Popen(
            [OLLAMA_PATH, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for it to be ready
        for _ in range(10):
            time.sleep(2)
            try:
                req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
                urllib.request.urlopen(req, timeout=3)
                return True
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return False


def call_ollama(prompt, model=None, max_tokens=8192, retries=3):
    """
    Call Ollama's /api/generate endpoint with JSON mode.

    Uses format="json" to guarantee valid JSON output.
    Timeout is 300s because local inference can be slow.
    Auto-starts Ollama if not running.
    """
    model = model or LLM_MODEL
    url = f"{OLLAMA_URL}/api/generate"

    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": max_tokens,
        },
    }).encode()
    headers = {"Content-Type": "application/json"}

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
            return data.get("response", "")
        except (urllib.error.URLError, ConnectionRefusedError):
            if attempt == 0:
                _log("Ollama not responding, attempting to start...")
                if _ensure_ollama_running():
                    _log("Ollama started successfully")
                    continue
                else:
                    _log("Failed to start Ollama")
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            _log(f"Ollama error (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(10)
    return ""


# ─── Gemini Provider ─────────────────────────────────────────────────────────

def call_gemini(prompt, max_tokens=8192, retries=3):
    """Call Google Gemini API (free tier)."""
    if not GEMINI_API_KEY:
        _log("GEMINI_API_KEY not set in .env")
        return ""

    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.1,
        },
    }).encode()
    headers = {"Content-Type": "application/json"}

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            content = (data.get("candidates", [{}])[0]
                       .get("content", {}).get("parts", [{}])[0].get("text", ""))
            return content
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 30 * (attempt + 1)
                _log(f"Gemini rate limit (429), waiting {wait}s...")
                time.sleep(wait)
            else:
                _log(f"Gemini HTTP error {e.code} (attempt {attempt + 1}/{retries})")
                time.sleep(5)
        except Exception as e:
            _log(f"Gemini error (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(5)
    return ""


# ─── Claude Provider ─────────────────────────────────────────────────────────

def call_claude(prompt, max_tokens=8192, retries=3):
    """
    Call Anthropic Claude API as a paid fallback.
    Requires ANTHROPIC_API_KEY in .env.
    """
    try:
        import anthropic
    except ImportError:
        _log("anthropic SDK not installed. Run: pip3 install anthropic")
        return ""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        _log("ANTHROPIC_API_KEY not set in .env")
        return ""

    client = anthropic.Anthropic(api_key=api_key)
    model = LLM_MODEL if "claude" in LLM_MODEL else "claude-sonnet-4-20250514"

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            if "rate" in str(e).lower():
                wait = 30 * (attempt + 1)
                _log(f"Claude rate limit, waiting {wait}s...")
                time.sleep(wait)
            else:
                _log(f"Claude error (attempt {attempt + 1}/{retries}): {e}")
                time.sleep(5)
    return ""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg):
    """Simple log to stdout."""
    print(f"[llm_provider] {msg}", flush=True)
