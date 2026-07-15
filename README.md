# simple-ralph-loop

`ralph` runs a prompt in a full-auto coding-agent session while refusing
metered LLM API authentication. The initial release supports macOS and
OpenCode authenticated with a ChatGPT subscription.

Install with `pipx install .`, then run with a finite budget of 1 to 100 fresh
sessions:

```sh
ralph run prompt.md --backend opencode --iterations 5
```

Use `--worktree PATH` to target another GitHub worktree and `--model
openai/MODEL` to override the default `openai/gpt-5.6-sol` model.

Ralph stops early on an exact completion marker. If the budget is exhausted,
it exits non-zero. Runtime state is private to the selected worktree's Git
directory. Remove only that state when no loop is active with:

```sh
ralph clean --worktree PATH
```

Full-auto mode can edit files and run commands without confirmation. Review
the prompt and repository before starting Ralph.
