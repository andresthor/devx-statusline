# devx-statusline

A statusline script for Claude Code. Two files do the work: `statusline/__main__.py`
renders, `statusline/costs.py` reads Claude Code's session logs and prices them.

Python 3.11+ (uses `tomllib`). No dependencies. No tests, no build step.

## Installing it for the user

```bash
cp -R statusline ~/.claude/hooks/devx-statusline
```

Then add to `~/.claude/settings.json` (merge into the existing JSON, don't
overwrite the file):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/hooks/devx-statusline"
  }
}
```

If `CLAUDE_CONFIG_DIR` is set, use that path instead of `~/.claude`.

Do **not** create a `config.toml` during install. Every setting has a working
default, and an empty config file is one more thing for the user to wonder
about. Only create one if they ask for a specific change.

Tell the user to start a new session — the statusline does not appear in the
session that installed it.

Then mention, in a sentence or two, that nothing needs configuring but a few
things can be. Keep it to the ones a first-time user actually reacts to:

- `instance_label` — a short label shown in orange on line 1. Lead with this
  one. If they run two Claude Code accounts, a personal and a work one, a
  different label in each config makes every window self-identifying. Unset by
  default, so it costs nothing to skip.
- `cost_window` — the second total on line 2. `"month"` (default), `"week"`,
  `"billing"`, or `""` to hide it.
- `day_start_hour` — when the daily counter resets. `6` makes late-night work
  count as the previous day.
- `[components]` — switches to hide any single element.

Say the rest is documented in `config.example.toml`, and that they can just ask
Claude Code to change a setting rather than editing TOML themselves. Don't
enumerate the full option list — that is what the example file is for.

## Verifying it works

The script reads one JSON object on stdin and prints two lines. To check an
install without starting a session:

```bash
echo '{"model":{"id":"claude-opus-5","display_name":"Opus 5"},
       "context_window":{"used_percentage":18,"context_window_size":1000000},
       "cost":{"total_cost_usd":4.2,"total_duration_ms":4920000,"total_api_duration_ms":810000},
       "workspace":{"current_dir":"'"$PWD"'"}}' | python3 ~/.claude/hooks/devx-statusline
```

Two lines of colored output means it's working. The script never raises — a
malformed or empty payload renders whatever it can and skips the rest. So if
the statusline is blank in Claude Code, the problem is the `settings.json`
path, not the script.

## The ⚠ indicator — what it means and how to clear it

A `⚠` at the far left of line 1 means the price list in `costs.py` needs a
human look. It is the only maintenance this project has. Two things trigger it:

**Red — an unrecognized model.** The user is on a model with no entry in
`_BASE_PRICING`, so costs are being estimated with the fallback rate and are
probably wrong. Fix it by adding the model's prefix and its input/output price
per million tokens:

```python
_BASE_PRICING: dict[str, tuple[float, float]] = {
    "claude-new-model-6": (5.0, 25.0),   # ← (input, output) USD per 1M tokens
    ...
}
```

**Red or yellow — a watch date.** Yellow means a dated pricing change lands
within two weeks; red means it already passed. The entries live in
`WATCH_DATES` in `costs.py`, each with a label saying what changes:

```python
WATCH_DATES: list[tuple[str, str]] = [
    ("2026-08-31", "Sonnet 5 intro pricing ends → reverts to $3/$15"),
]
```

Apply the change the label describes, then delete that entry. If no entries
remain, leave the list empty — the indicator stays hidden.

**Get prices from the source, not from memory.** Read
https://platform.claude.com/docs/en/about-claude/pricing (or load the
`claude-api` skill if it is available) and use the published numbers. Model
prices change and a guess here silently corrupts every total the user sees.

Cache and fast-mode rates are derived from the base input rate by the
multipliers at the top of `costs.py` — you do not need to add those per model.
Only `_FAST_PRICING` is a separate table, and only for models with a fast tier.

## Design constraints

Keep these in mind before adding anything:

- **Runs on every render.** Anything slow shows up as terminal lag. The JSONL
  parsing is cached by file mtime; keep it that way.
- **Never raise.** A traceback would replace the user's statusline with an
  error. Every I/O path is already wrapped; new ones should be too.
- **No network calls, ever.** Costs are computed locally from a static price
  table. The user is told this in the README and it should stay true.
- **No Nerd Font glyphs.** Everything on screen is standard Unicode so it
  renders in any terminal. `⎇ ◼ ◻ ═ ─ ◷ ◉ ○◔◑◕● Σ ⚠ •` are all safe; private
  use area codepoints are not.

## Layout

```
Line 1:  [⚠]  [context]  [turns $session]  [usage or duration]  • [model effort]  [label]  [◷ last reply]
Line 2:  [cwd]  [branch]  [worktree]  [pr]  • [Σ today]  • [Σ window]  • [↑ behind]
```

The model name on line 1 carries the instance label's orange, except for corti
models — matched by a substring of `model.display_name`, not the id — which take
a brand lime. The effort meter beside it inherits whichever color applied.

`render_usage_block()` picks between two things for the middle of line 1: the
plan's published usage limits when the payload has `rate_limits`, and
wall-clock plus API-time bars for the current session when it doesn't.
Enterprise plans generally fall into the second case.

The `[cwd]` slot is dropped in a worktree — the directory is named after the
branch, so both would say the same thing — which makes `_worktree_name()`
load-bearing for more than the worktree slot itself.

The `[worktree]` and `[pr]` slots on line 2 appear only when the payload reports
them — the worktree name when the session runs in a linked worktree, and the
current branch's PR review state (linked to its URL) otherwise. Both degrade to
nothing when absent, so line 2 stays uncluttered in a plain repo with no PR.
The worktree name is bracketed and dimmed as a qualifier on whatever precedes
it, and takes the branch glyph with cwd's color, cap and slot when nothing does.

## The update check

`commits_behind()` reports how far the checkout this script runs from is behind
its upstream, rendered as `↑ N` at the far right of line 2. It reads git's refs
off disk — `HEAD`, the branch ref, and `refs/remotes/origin/<branch>`, falling
back to `packed-refs` — so the usual case of matching SHAs answers without
spawning anything. Only a divergence runs `git rev-list --count`, and that
result is cached in the temp dir against both SHAs.

Nothing here fetches, which keeps the no-network rule intact but means the
indicator is only as current as the last `git fetch` from somewhere else. A
copy install has no `.git` and reports nothing, which is the correct answer for
a directory that has no upstream.

`__version__` in `__main__.py` is not rendered anywhere — it exists to tag
releases and to tell copies apart. Bump it in the same commit as the change it
describes.

## Config

Optional TOML, read from `statusline/config.toml` first, then
`$CLAUDE_CONFIG_DIR/statusline.toml`. Every key is documented in
`config.example.toml`, and every key has a default in the code — adding a new
one means adding it to both places.
