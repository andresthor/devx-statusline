# devx-statusline

Live context, cost, and session timing in your Claude Code statusline.

```
  18% ◼◼◼◼◻◻ ◻◻◻◻ 185k/1M  12t $4.2  ◷ ══─── 1:22 • ◉ ══─── 13m • Opus 4.8 high  work  ◷ 14:07
  ~/projects/my-app  ⎇ main • Σ $19.1·day • Σ $284.0·mo
```

## Features

- **Two lines instead of one** — session state on top, location and spend below
- **Context bar that splits at 256k** on 1M-token models, so a normal session
  moves the bar instead of sitting at one filled block all day
- **Costs computed locally** from Claude Code's own session logs — no network
  calls, ever
- **11 models in the built-in price table**, with cache and fast-mode rates
  derived automatically from each base rate
- **Three cost horizons** — this session, today, and a rolling 30-day, 7-day, or
  billing-period window
- **Works on plans without published usage limits** — falls back to wall-clock
  and API-time bars for the current session
- **Per-account labels** — run a personal and a work account side by side and
  tell their windows apart at a glance
- **Never raises** — a malformed payload drops one element, it doesn't replace
  your statusline with a traceback
- **Python 3.11+, standard library only** — no dependencies, no build step, no
  Nerd Font

## Install

Ask Claude Code to do it:

> Install the statusline from https://github.com/YOUR-ORG/devx-statusline

Or do it by hand — copy the folder and point Claude Code at it:

```bash
git clone https://github.com/YOUR-ORG/devx-statusline
cp -R devx-statusline/statusline ~/.claude/hooks/devx-statusline
```

Then add this to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/hooks/devx-statusline"
  }
}
```

Start a new Claude Code session and the statusline appears. There is no config
file to create — everything has a working default.

## Reading it

**Line 1 — how this session is going.**

| Element | Meaning |
| --- | --- |
| `18% ◼◼◼◼◻◻ ◻◻◻◻` | Context used. Filled blocks go green → yellow → orange → red as it fills. |
| `185k/1M` | Tokens used out of the model's context window. |
| `12t` | Messages you've sent in this session. |
| `$4.2` | What this session has cost so far. |
| `◷ ══─── 1:22` | How long the session has been open (wall clock). |
| `◉ ══─── 13m` | How much of that was spent waiting on Claude. |
| `Opus 4.8 high` | The model, and its reasoning effort level. |
| `work` | Your label for this account, in orange. Off unless you set one. |
| `◷ 14:07` | When Claude last replied. |

**The label is for running more than one account.** If you have a personal
Claude Code account and a work one, give each a different `instance_label` and
every window tells you which is which. With one account, leave it unset and
nothing shows.

On plans that publish usage limits, the two duration bars are replaced by your
actual limit usage — a 5-hour window and a 7-day window, each with a percentage.
On plans without published limits (enterprise, typically) you get the session
duration bars shown above.

**Line 2 — where you are and what you're spending.**

| Element | Meaning |
| --- | --- |
| `~/projects/my-app` | Working directory. Clickable in most modern terminals. |
| `⎇ main` | Current git branch. |
| `Σ $19.1·day` | Everything you've spent today, across all projects. |
| `Σ $284.0·mo` | Same, for the last 30 days. |

Occasionally a `⚠` appears at the far left of line 1. That means the built-in
price list needs updating — see [CLAUDE.md](CLAUDE.md), or just tell Claude
Code "the statusline is showing a warning" and it will handle it.

## Where the cost numbers come from

Claude Code writes a log of every session to `~/.claude/projects`. This script
reads those logs, adds up the tokens, and multiplies by the published per-model
prices. Nothing leaves your machine, and nothing is sent to Anthropic or
anywhere else.

Two consequences worth knowing:

- The totals are an **estimate**. They should track your real bill closely, but
  they are computed locally from published rates, not read from your account.
- Prices change. When Anthropic adjusts a rate or releases a model this script
  hasn't seen, the `⚠` appears. Fixing it is a one-line edit that Claude Code
  can make for you.

## Configuration

Optional. If the defaults suit you, skip this section entirely.

To change something, copy `config.example.toml` to `config.toml` inside the
installed folder and uncomment what you need:

```bash
cp ~/.claude/hooks/devx-statusline/config.example.toml \
   ~/.claude/hooks/devx-statusline/config.toml
```

The most likely things to want:

| Setting | Default | What it does |
| --- | --- | --- |
| `instance_label` | unset | Short orange label on line 1. Set a different one per account to tell them apart. |
| `cost_window` | `"month"` | The second total on line 2. `"month"`, `"week"`, `"billing"`, or `""` to hide it. |
| `day_start_hour` | `0` | Hour the daily counter resets. Set to `6` so late-night work counts as the previous day. |
| `cumulative_cost_scale` | `100.0` | Dollar amount where the line-2 totals turn fully red. |
| `[components]` | all on | Turn individual pieces off — see the example file for the full list. |

Every option is documented inline in `config.example.toml`.

## Layout

```
Line 1:  [⚠]  [context]  [turns $session]  [usage or duration]  • [model effort]  [label]  [◷ last reply]
Line 2:  [cwd]  [branch]  • [Σ today]  • [Σ window]
```

## Uninstall

Remove the `statusLine` block from `~/.claude/settings.json` and delete
`~/.claude/hooks/devx-statusline`.

## License

MIT
