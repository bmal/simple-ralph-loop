"""simple-ralph-loop: a macOS-only loop runner that repeatedly executes one prompt
in fresh full-auto coding-agent sessions against a finite Iteration budget.

Module map — start here, then open only the module you need:

- ``cli``          — argument parsing and the run/clean/resume commands; ``main``.
- ``loop``         — the budgeted Iteration loop, handoff printing, outcome recording.
- ``launch``       — the Launch chain: wrapped argv, the power-assertion seam and
                     ``CaffeinateAssertion``, host-isolation profile + sandbox wrap,
                     and recovery-command (resume/restart) formatting.
- ``backends``     — the Backend package: per-backend default models, the five-name
                     Backend Protocol, and the registry that resolves a name to its
                     adapter (the one backend-name resolution).
- ``backends.opencode`` — the OpenCode adapter: preflight, agent refusal, isolated
                     config, event accumulation, iteration incl. second-pass
                     verification, session persistence.
- ``backends.claude``   — the Claude adapter: preflight, customization refusal,
                     Claude constants and host paths, event accumulation, iteration,
                     session persistence.
- ``protocol``     — the Loop protocol text and completion/needs-input detection.
- ``process``      — process-group control, timeouts, controlled-stop classification,
                     process identity.
- ``locking``      — Git-private state directories, the worktree lock, lock metadata.
- ``gitcontext``   — the subprocess helper, prompt reading, git/GitHub context,
                     redacted JSON artifact writing.
- ``environment``  — the sanitized session environment, banned LLM env vars, the
                     unsafe-environment refusal.
- ``preflight``    — backend-agnostic Trust boundary checks shared by the adapters.
- ``redaction``    — secret collection, the ``Redactor``, and the active-redactor
                     functions (import the functions, never the global).
- ``errors``       — ``RalphError``, ``HandoffError``, ``StartedIterationError``.

See CONTEXT.md for the vocabulary (Backend, Iteration, Launch chain, Loop protocol,
Trust boundary, Handed-off session) these modules are named after.
"""

__version__ = "0.1.0"
