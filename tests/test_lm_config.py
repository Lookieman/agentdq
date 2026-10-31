# ---------------------------------------------------------------------------
# tests/test_lm_config.py
# v1.0 | 11-Aug-2026 | Package 4g. The model string resolution order: the CLI
#                      flag wins, then AGENTDQ_MODEL, then the default. No DSPy
#                      import is needed, so this stays offline.
# ---------------------------------------------------------------------------

from __future__ import annotations

from src.dspy_modules.lm_config import DEFAULT_MODEL, MODEL_ENV_VAR, resolve_model


def test_the_cli_flag_wins(monkeypatch):
    monkeypatch.setenv(MODEL_ENV_VAR, "openai/from-env")
    assert resolve_model("openai/from-cli") == "openai/from-cli"


def test_the_environment_beats_the_default(monkeypatch):
    monkeypatch.setenv(MODEL_ENV_VAR, "ollama_chat/local-model")
    assert resolve_model(None) == "ollama_chat/local-model"


def test_the_default_is_the_model_the_suggester_already_ran(monkeypatch):
    monkeypatch.delenv(MODEL_ENV_VAR, raising=False)
    assert resolve_model(None) == DEFAULT_MODEL
    assert DEFAULT_MODEL == "openai/gpt-4o-mini"
