# ---------------------------------------------------------------------------
# src/dspy_modules/lm_config.py
# v1.0 | 11-Aug-2026 | Package 4g. One place that configures the language model
#                      for EVERY DSPy program in the repository. Before this,
#                      configure_lm lived inside tools/run_suggestion.py, and a
#                      second caller (the adjudicator) would have copied it.
# ---------------------------------------------------------------------------
"""Shared language-model setup.

The model string resolves in this order:

    1. The value the caller passes (a CLI --model flag).
    2. The AGENTDQ_MODEL variable from the environment or a .env file.
    3. DEFAULT_MODEL.

The API key is read from the environment, never from a flag, so it cannot land
in shell history or an artefact. Package 6 swaps the default for a local model
string ("ollama_chat/<name>"), and nothing else changes.

Temperature is 0.0. The adjudicator's verdicts go into findings and scores, so
a run must be repeatable: the same pair, the same model and the same settings
must give the same answer.
"""

from __future__ import annotations

import os
from typing import Optional

# The default: the model the suggestion pipeline already ran, cheap enough for
# a 500-pair adjudication run. Alternatives: "openai/gpt-4o" (better judgement,
# about 15x the cost, not needed for a baseline); a local string via Package 6.
DEFAULT_MODEL: str = "openai/gpt-4o-mini"

MODEL_ENV_VAR: str = "AGENTDQ_MODEL"


def resolve_model(cli_model: Optional[str] = None) -> str:
    """Return the model string to use, without touching DSPy.

    Kept separate from configure_lm so the resolution order is testable with no
    model library installed.
    """
    if cli_model:
        return cli_model
    return os.environ.get(MODEL_ENV_VAR, "") or DEFAULT_MODEL


def configure_lm(model: Optional[str] = None) -> str:
    """Load .env, configure DSPy with the resolved model, return the string."""
    import dspy  # heavy import kept local

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        # python-dotenv is optional; the environment can be set another way.
        pass

    resolved: str = resolve_model(model)
    dspy.configure(lm=dspy.LM(resolved, temperature=0.0))
    return resolved
