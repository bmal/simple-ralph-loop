"""The sanitized session environment, the banned LLM env vars, and the
unsafe-environment refusal.

Invariants:
- ``LLM_ENV_VARS`` is the ban-list of API-key and custom-endpoint variables that
  would let a backend session bill a metered API or reroute off the operator's
  subscription. ``clean_environment`` strips every one of them from the child
  environment, and ``reject_unsafe_environment`` fails closed if any is set (or if
  a config-dir override would make routing ambiguous) before budget is spent.
- ``BACKEND_TIMEOUT_MS`` pins the backend's own request/Bash limits to a 32-bit
  ceiling so a positive Ralph timeout always stays authoritative (see
  ``MAX_ITERATION_TIMEOUT_SECONDS`` in ``process``); the backend limit only bites
  when Ralph's own timer is explicitly disabled.

Depends on / must not know: ``errors`` only, at module load. ``clean_environment``
imports ``backends.opencode.isolated_config`` lazily inside the function — the
per-backend branch here is transitional (register E8 commit 1 keeps the dispatch;
commit 2 dissolves it into each adapter's ``environment``), and the lazy import
keeps this near-leaf module out of the ``backends`` import cycle. It must not know
how a Backend consumes the environment beyond the two keys it sets.

See also: ``redaction`` (derives its secret set from ``LLM_ENV_VARS``),
``backends.opencode`` (owns ``isolated_config``), ``preflight``.
"""

from __future__ import annotations

import json
import os

from .errors import RalphError


# Backend request and Bash-tool timeouts are configured in integer
# milliseconds and are bounded by a signed 32-bit value, so they can never be
# made truly infinite. Ralph pins them to this ceiling and caps its own
# accepted iteration timeout well below it (see MAX_ITERATION_TIMEOUT_SECONDS)
# so a positive Ralph timeout is always authoritative and the backend limit
# only becomes relevant when Ralph's own timer is explicitly disabled.
BACKEND_TIMEOUT_MS = 2147483647
LLM_ENV_VARS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENCODE_API_KEY",
    "OPENCODE_MODELS_URL",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
}


def clean_environment(model: str, backend: str) -> dict[str, str]:
    from .backends.opencode import isolated_config

    env = {key: value for key, value in os.environ.items() if key not in LLM_ENV_VARS}
    ceiling = str(BACKEND_TIMEOUT_MS)
    if backend == "opencode":
        env.update(
            {
                "OPENCODE_CONFIG_CONTENT": json.dumps(isolated_config(model), separators=(",", ":")),
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": ceiling,
            }
        )
    else:
        env.update(
            {
                "API_TIMEOUT_MS": ceiling,
                "BASH_DEFAULT_TIMEOUT_MS": ceiling,
                "BASH_MAX_TIMEOUT_MS": ceiling,
                "DISABLE_AUTOUPDATER": "1",
            }
        )
    return env


def reject_unsafe_environment() -> None:
    if any(os.environ.get(name) for name in LLM_ENV_VARS):
        raise RalphError("LLM API credential or custom endpoint environment is not allowed")
    for name in ("CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        if os.environ.get(name):
            raise RalphError(f"{name} is not allowed because routing would be ambiguous")
