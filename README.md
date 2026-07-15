# simple-ralph-loop

`ralph` runs a prompt in a full-auto coding-agent session while refusing
metered LLM API authentication. The initial release supports macOS and
OpenCode authenticated with a ChatGPT subscription.

Install with `pipx install .`, then run one iteration:

```sh
ralph run prompt.md --backend opencode --iterations 1
```

Use `--worktree PATH` to target another GitHub worktree and `--model
openai/MODEL` to override the default `openai/gpt-5.6-sol` model.

Full-auto mode can edit files and run commands without confirmation. Review
the prompt and repository before starting Ralph.
