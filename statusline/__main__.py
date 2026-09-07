#!/usr/bin/env python3
"""
A two-line statusline for Claude Code.

Line 1:  [⚠]  [ctx% bar tokens]  [Nt $sess]  [usage or session duration]  [• model]  [◷ time]
Line 2:  [~/cwd]  [⎇ branch]  [Σ $today]  [Σ $window]

Config is optional — every value below has a working default. To override,
copy config.example.toml to config.toml next to this file, or drop a
statusline.toml in your Claude config directory (~/.claude by default).
"""

import calendar
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .costs import (
        cumulative_cost,
        due_watch_dates,
        is_known_model,
        last_assistant_time,
        last_context_tokens,
        session_turns,
        transcript_for,
    )
except ImportError:
    from costs import (
        cumulative_cost,
        due_watch_dates,
        is_known_model,
        last_assistant_time,
        last_context_tokens,
        session_turns,
        transcript_for,
    )


# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
# Where the script was invoked from, symlinks left unresolved: an install that
# links the source files into place keeps its config here, out of the checkout.
LINK_DIR = Path(__file__).parent


def _config_paths() -> list[Path]:
    paths = [
        SCRIPT_DIR / "config.toml",  # next to the source
        CLAUDE_DIR / "statusline.toml",  # user-global fallback
        LINK_DIR / "config.toml",  # next to the symlink, if it is its own dir
    ]
    seen: set[str] = set()
    unique = []
    for path in paths:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


CONFIG_PATHS = _config_paths()


def load_config() -> dict:
    for path in CONFIG_PATHS:
        if path.exists():
            try:
                with open(path, "rb") as f:
                    return tomllib.load(f)
            except Exception:
                continue
    return {}


def show(cfg: dict, name: str) -> bool:
    """Whether a named component is enabled. Omitted keys default to on."""
    return cfg.get("components", {}).get(name, True)


def num(value, default, cast=int):
    """Coerce a payload value to a number, falling back on anything unexpected.

    Payload fields are read defensively so a renamed, retyped, or missing
    field degrades that one element instead of replacing the whole statusline
    with a traceback.
    """
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def text(value, default: str = "") -> str:
    """Coerce a payload value to a string. See ``num`` for the rationale."""
    return value if isinstance(value, str) else default


def sub(data: dict, key: str) -> dict:
    """A nested payload object, or an empty dict if absent or not an object.

    ``data.get(key, {})`` returns None when the key is present but null, which
    then raises on the next lookup. See ``num`` for the rationale.
    """
    value = data.get(key)
    return value if isinstance(value, dict) else {}


# ── Catppuccin Mocha palette ──────────────────────────────────────────────────


class C:
    OVERLAY2 = (147, 153, 178)
    OVERLAY1 = (127, 132, 156)
    OVERLAY0 = (108, 112, 134)
    SURFACE2 = (88, 91, 112)
    GREEN = (166, 227, 161)
    GREEN_DIM = (106, 141, 110)
    BLUE = (137, 180, 250)
    SAPPHIRE = (116, 199, 236)
    YELLOW = (249, 226, 175)
    PEACH = (250, 179, 135)
    RED = (243, 139, 168)
    MAROON = (235, 160, 172)
    DEEP_RED = (170, 55, 80)
    CLAUDE_ORANGE = (215, 119, 87)


RESET = "\033[0m"
BOLD = "\033[1m"


def fg(*rgb):
    return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


# ── Update-needed indicator ──────────────────────────────────────────────────
# Silent unless the pricing table in costs.py needs a human look: an
# unrecognized model (costs are being guessed) or a maintenance watch-date
# coming due. Bare glyph — color is the signal (red = now, yellow = soon).


def _update_colors(model_id: str) -> list[tuple]:
    colors: list[tuple] = []
    if model_id and not is_known_model(model_id):
        colors.append(C.RED)
    today = datetime.now().date()
    for iso, _label in due_watch_dates():
        overdue = datetime.fromisoformat(iso).date() < today
        colors.append(C.RED if overdue else C.YELLOW)
    return colors


def update_indicator(model_id: str) -> str:
    colors = _update_colors(model_id)
    if not colors:
        return ""
    return f"{fg(*colors[0])}{BOLD}⚠{RESET}"


# ── Context bar ───────────────────────────────────────────────────────────────

WORKING_ZONE_TOKENS = 256_000

# Per-block color palettes — warm gradient from dark green through to maroon
WORKING_BLOCK_COLORS = [C.GREEN_DIM, C.GREEN, C.YELLOW, C.PEACH, C.RED, C.MAROON]
OVERFLOW_BLOCK_COLORS = [C.RED, C.MAROON, C.DEEP_RED, C.DEEP_RED]
LINEAR_BLOCK_COLORS = [
    C.GREEN_DIM,
    C.GREEN,
    C.YELLOW,
    C.PEACH,
    C.CLAUDE_ORANGE,
    C.RED,
    C.MAROON,
    C.DEEP_RED,
]


def working_zone_color(pct: float) -> tuple:
    """Color for 0-100% of the working zone (0-256k)."""
    if pct < 20:
        return C.GREEN_DIM
    elif pct < 40:
        return C.GREEN
    elif pct < 60:
        return C.YELLOW
    elif pct < 80:
        return C.PEACH
    else:
        return C.RED


def linear_ctx_color(pct: float) -> tuple:
    """Color for 0-100% of full context (models ≤256k)."""
    if pct < 15:
        return C.GREEN_DIM
    elif pct < 30:
        return C.GREEN
    elif pct < 50:
        return C.YELLOW
    elif pct < 65:
        return C.PEACH
    elif pct < 80:
        return C.RED
    else:
        return C.MAROON


def _render_bar(filled: int, width: int, colors: list[tuple], per_block: bool) -> str:
    """Render a bar with per-block or uniform coloring."""
    if per_block:
        parts = []
        for i in range(width):
            if i < filled:
                parts.append(f"{fg(*colors[i])}◼")
            else:
                parts.append(f"{fg(*C.SURFACE2)}◻")
        return "".join(parts)
    # Uniform: use the color of the highest filled block
    color = colors[max(0, filled - 1)] if filled > 0 else C.SURFACE2
    return f"{fg(*color)}{'◼' * filled}{fg(*C.SURFACE2)}{'◻' * (width - filled)}"


def context_bar_split(
    used_tokens: int,
    ctx_size: int,
    per_block: bool = True,
    working_width: int = 6,
    overflow_width: int = 4,
) -> str:
    """Two-zone bar: 6 working blocks (0-256k) + space + 4 overflow blocks."""
    w_pct = min(1.0, used_tokens / WORKING_ZONE_TOKENS)
    w_filled = max(0, min(working_width, round(w_pct * working_width)))

    if used_tokens > WORKING_ZONE_TOKENS and ctx_size > WORKING_ZONE_TOKENS:
        o_pct = min(
            1.0, (used_tokens - WORKING_ZONE_TOKENS) / (ctx_size - WORKING_ZONE_TOKENS)
        )
        o_filled = max(0, min(overflow_width, round(o_pct * overflow_width)))
    else:
        o_filled = 0

    working_s = _render_bar(w_filled, working_width, WORKING_BLOCK_COLORS, per_block)
    overflow_s = _render_bar(o_filled, overflow_width, OVERFLOW_BLOCK_COLORS, per_block)
    return f"{working_s} {overflow_s}{RESET}"


def context_bar_linear(used_pct: float, per_block: bool = True, width: int = 8) -> str:
    """Linear bar for models with context ≤256k."""
    filled = max(0, min(width, round(used_pct / 100 * width)))
    return f"{_render_bar(filled, width, LINEAR_BLOCK_COLORS, per_block)}{RESET}"


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 100_000:
        v = n / 1000
        return f"{v:.0f}k" if v == int(v) else f"{v:.1f}k"
    if n >= 1_000_000:
        m = n / 1_000_000
        return f"{m:.0f}M" if m == int(m) else f"{m:.1f}M"
    return f"{n // 1000}k"


# ── Cost ──────────────────────────────────────────────────────────────────────

SESSION_COST_SCALE = 10.0  # $10 session = fully red
CUMULATIVE_COST_SCALE = 100.0  # $100 cumulative = fully red


def cost_color(amount: float, scale: float = 10.0) -> tuple:
    pct = amount / scale
    if pct < 0.10:
        return C.GREEN_DIM
    elif pct < 0.20:
        return C.GREEN
    elif pct < 0.50:
        return C.YELLOW
    elif pct < 1.00:
        return C.PEACH
    else:
        return C.RED


def fmt_cost(amount: float, scale: float = 10.0) -> str:
    col = cost_color(amount, scale)
    return f"{fg(*col)}${amount:.1f}{RESET}"


def turns_color(n: int) -> tuple:
    if n < 7:
        return C.GREEN_DIM
    elif n < 14:
        return C.GREEN
    elif n < 21:
        return C.YELLOW
    elif n < 28:
        return C.PEACH
    elif n < 35:
        return C.CLAUDE_ORANGE
    elif n < 42:
        return C.RED
    elif n < 49:
        return C.MAROON
    else:
        return C.DEEP_RED


# ── Usage bars ────────────────────────────────────────────────────────────────


def usage_color(pct: float) -> tuple:
    if pct < 30:
        return C.GREEN_DIM
    elif pct < 50:
        return C.GREEN
    elif pct < 70:
        return C.YELLOW
    elif pct < 85:
        return C.PEACH
    elif pct < 95:
        return C.RED
    else:
        return C.DEEP_RED


_TIME_PIES = "○◔◑◕●"


def time_pie(resets_at: int, window_seconds: int) -> str:
    """Circle pie showing elapsed fraction of a time window."""
    remaining = max(0, resets_at - int(datetime.now().timestamp()))
    elapsed_frac = 1.0 - (remaining / window_seconds) if window_seconds else 1.0
    elapsed_frac = max(0.0, min(1.0, elapsed_frac))
    idx = min(len(_TIME_PIES) - 1, int(elapsed_frac * len(_TIME_PIES)))
    return _TIME_PIES[idx]


def usage_bar(pct: float, width: int = 5) -> str:
    """Box-drawing progress bar: ═══── style."""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "═" * filled + "─" * (width - filled)


def fmt_duration(ms: int) -> str:
    """Compact duration: 45s / 12m / 2:34."""
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}:{m:02d}"


# Reference points for the session-duration bars (shown when the plan exposes
# no rate limits, e.g. enterprise). Neither is a real limit — they just set
# where each bar reads as "full".
SESSION_WALL_SCALE_SEC = 4 * 3600  # 4h wall-clock
SESSION_API_SCALE_SEC = 30 * 60  # 30m waiting on the API


# ── Billing periods ───────────────────────────────────────────────────────────


def billing_cutoff(cfg: dict) -> datetime | None:
    """Compute the start of the current billing period from config."""
    now = datetime.now()

    billing_start = cfg.get("billing_start", "")
    if billing_start:
        anchor = datetime.fromisoformat(billing_start)
        # Convert to local time, then strip tz for naive comparison
        cutoff = anchor.astimezone().replace(tzinfo=None)
        # If anchor is in the future, walk backward to find current period
        while cutoff > now:
            m, y = cutoff.month - 1, cutoff.year
            if m < 1:
                m, y = 12, y - 1
            try:
                cutoff = cutoff.replace(year=y, month=m)
            except ValueError:
                last = calendar.monthrange(y, m)[1]
                cutoff = cutoff.replace(year=y, month=m, day=last)
        # Walk forward to find the most recent period start <= now
        while True:
            y, m = cutoff.year, cutoff.month + 1
            if m > 12:
                m, y = 1, y + 1
            try:
                next_cutoff = cutoff.replace(year=y, month=m)
            except ValueError:
                last = calendar.monthrange(y, m)[1]
                next_cutoff = cutoff.replace(year=y, month=m, day=last)
            if next_cutoff > now:
                return cutoff
            cutoff = next_cutoff

    billing_day = cfg.get("billing_day")
    if billing_day:
        day = int(billing_day)
        cutoff = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
        if cutoff > now:
            m, y = now.month - 1, now.year
            if m < 1:
                m, y = 12, y - 1
            try:
                cutoff = cutoff.replace(year=y, month=m)
            except ValueError:
                last = calendar.monthrange(y, m)[1]
                cutoff = cutoff.replace(year=y, month=m, day=last)
        return cutoff

    return None


def _cost_cutoff(window: str, cfg: dict) -> datetime | None:
    """Compute the cutoff datetime for a given cost window."""
    now = datetime.now()
    if window == "billing":
        return billing_cutoff(cfg)
    return {
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
    }.get(window)


# ── Path / git ────────────────────────────────────────────────────────────────


# Line 2 grows with the length of a branch name, and a worktree named after a
# long branch pushes the cost totals off screen entirely. Each slot is capped
# independently; a limit under 4 leaves no room for an ellipsis and reads as
# "off".
CWD_MAX_LENGTH = 40
BRANCH_MAX_LENGTH = 32
WORKTREE_MAX_LENGTH = 24


def abbreviate(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def clip(s: str, limit: int, middle: bool = False) -> str:
    """Shorten to `limit` characters with an ellipsis, from the end or middle.

    Middle-clipping keeps the tail, where branch-derived names carry the ticket
    id that tells two otherwise similar names apart.
    """
    if limit < 4 or len(s) <= limit:
        return s
    keep = limit - 1
    if not middle:
        return s[:keep] + "…"
    head = (keep + 1) // 2
    return s[:head] + "…" + s[len(s) - (keep - head) :]


def shorten_path(path: str, limit: int) -> str:
    """Drop leading path segments so the current directory stays readable.

    Only the deepest segment is guaranteed; parents are kept while they fit.
    A lone segment over the limit is clipped rather than dropped, since there
    is nothing else left to identify the directory by.
    """
    if limit < 4 or len(path) <= limit:
        return path
    parts = path.split("/")
    kept = [parts[-1]]
    if len(parts[-1]) + 2 > limit:
        # Keep the "…/" so the slot still reads as a path, unless the limit is
        # too tight to spend two characters on it.
        prefix = "…/" if limit >= 6 else ""
        return prefix + clip(parts[-1], limit - len(prefix), middle=True)
    for part in reversed(parts[:-1]):
        if len("/".join([part, *kept])) + 2 > limit:
            break
        kept.insert(0, part)
    return "…/" + "/".join(kept)


def osc8_link(uri: str, label: str) -> str:
    """OSC 8 clickable hyperlink (Ghostty / iTerm2 / Kitty / WezTerm)."""
    return f"\033]8;;{uri}\a{label}\033]8;;\a"


# ── Pull request ───────────────────────────────────────────────────────────────
# Shown on line 2 when a PR exists for the current branch. The glyph carries
# the review state; the color reinforces it. Absent (no PR / not in a repo)
# degrades to nothing — the standard defensive-read behavior.

_PR_GLYPH = {
    "approved": "✓",
    "changes_requested": "↻",
    "pending": "⧖",
    "draft": "☐",
}
_PR_COLOR = {
    "approved": C.GREEN,
    "changes_requested": C.PEACH,
    "pending": C.YELLOW,
    "draft": C.OVERLAY0,  # muted — a draft isn't in flight
}


def pr_block(data: dict) -> str:
    """PR status for the current branch, or "" if none is reported.

    The whole ``pr`` object is absent until Claude Code finds a PR for the
    branch, and ``review_state`` can be independently absent even then. Both
    gaps fall through to an empty string — no glyph is shown without a state.
    """
    pr = sub(data, "pr")
    number = pr.get("number")
    if not number:
        return ""
    url = text(pr.get("url"))
    state = text(pr.get("review_state"))
    glyph = _PR_GLYPH.get(state)
    if not glyph:
        # PR exists but no review state yet — show the number unmarked rather
        # than inventing a state. Still clickable, still colored neutral.
        colored = f"{fg(*C.OVERLAY2)}#{number}{RESET}"
    else:
        color = _PR_COLOR[state]
        colored = f"{fg(*color)}{glyph}{RESET} {fg(*color)}#{number}{RESET}"
    if url:
        return osc8_link(url, colored)
    return colored


# ── Worktree ───────────────────────────────────────────────────────────────────
# Shown on line 2 when the session runs in a linked git worktree. Brackets
# carry the qualifier meaning ("the branch above is checked out here"), so no
# glyph is used — and they drop when there is nothing above to qualify. The
# name is suppressed when it equals the branch — a common case
# (`git worktree add ../fix-bug fix-bug`) where showing both is noise.

def _worktree_name(data: dict) -> str:
    """Best-effort worktree name, or "" when not in a worktree.

    ``worktree.*`` (only present in --worktree sessions) carries a clean
    ``name``. A plain ``git worktree add`` exposes only ``workspace.git_worktree``.
    The main working tree has neither and returns "".
    """
    wt = sub(data, "worktree")
    name = text(wt.get("name"))
    if name:
        return name
    raw = text(sub(data, "workspace").get("git_worktree"))
    if not raw:
        return ""
    # Documented as a name, but a path would render the whole thing on line 2.
    return os.path.basename(raw.rstrip("/")) or ""


def worktree_block(
    data: dict, branch: str | None, limit: int, standalone: bool = False
) -> str:
    """Worktree name for line 2, or "" if none / name == branch.

    ``standalone`` means nothing precedes it on line 2 to qualify, so the name
    is the location rather than a note about one: it takes the branch glyph and
    cwd's color, and its cap from the caller, losing the brackets and dimming
    that mark it secondary.
    """
    name = _worktree_name(data)
    if not name or name == branch:
        return ""
    if standalone:
        return f"{fg(*C.BLUE)}⎇ {clip(name, limit, middle=True)}{RESET}"
    return f"{fg(*C.OVERLAY0)}[{clip(name, limit)}]{RESET}"


def git_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None
    except Exception:
        return None


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_usage_block(data: dict, cfg: dict, dot: str) -> str:
    """Plan usage if the payload reports it, else session duration.

    Enterprise and other plans without published rate limits send no
    `rate_limits`, so there is nothing to count down. Those fall through to
    wall-clock and API-time bars for the current session instead.
    """
    rate_limits = sub(data, "rate_limits")
    five_hour = sub(rate_limits, "five_hour")
    seven_day = sub(rate_limits, "seven_day")

    parts = []
    if five_hour:
        pct = num(five_hour.get("used_percentage"), 0, float)
        pie = time_pie(num(five_hour.get("resets_at"), 0), 5 * 3600)
        parts.append(
            f"{fg(*C.GREEN)}{pie}{RESET} "
            f"{fg(*usage_color(pct))}{usage_bar(pct)} {pct:.0f}%{RESET}"
        )
    if seven_day:
        pct = num(seven_day.get("used_percentage"), 0, float)
        parts.append(
            f"{fg(*C.YELLOW)}7d{RESET} "
            f"{fg(*usage_color(pct))}{usage_bar(pct)} {pct:.0f}%{RESET}"
        )
    if parts:
        return dot.join(parts)

    cost_data = sub(data, "cost")
    wall_ms = num(cost_data.get("total_duration_ms"), 0)
    api_ms = num(cost_data.get("total_api_duration_ms"), 0)
    if wall_ms <= 0:
        return ""

    wall_scale = num(cfg.get("session_wall_scale_seconds"), SESSION_WALL_SCALE_SEC)
    api_scale = num(cfg.get("session_api_scale_seconds"), SESSION_API_SCALE_SEC)
    wall_pct = min(100.0, wall_ms / 1000 / wall_scale * 100)
    api_pct = min(100.0, api_ms / 1000 / api_scale * 100)
    return (
        f"{fg(*C.SAPPHIRE)}◷{RESET} "
        f"{fg(*usage_color(wall_pct))}{usage_bar(wall_pct)} {fmt_duration(wall_ms)}{RESET}"
        f"{dot}{fg(*C.PEACH)}◉{RESET} "
        f"{fg(*usage_color(api_pct))}{usage_bar(api_pct)} {fmt_duration(api_ms)}{RESET}"
    )


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    ctx = sub(data, "context_window")
    # Payload first. When it is absent the window is still knowable: Claude Code
    # exports CLAUDE_CODE_MAX_CONTEXT_TOKENS to the subprocesses it spawns, and
    # for a proxied model that is the only place its real window appears.
    ctx_size = num(ctx.get("context_window_size"), 0)
    if not ctx_size:
        ctx_size = num(os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"), 200000)
    # total_input_tokens is the exact context occupancy across all token types.
    # used_percentage is rounded to a whole percent, which is a 10k-token
    # quantum at a 1M window — too coarse to derive a token count from.
    # Older builds send only the percentage, so keep deriving when it's absent.
    exact_tokens = num(ctx.get("total_input_tokens"), 0)
    if exact_tokens and ctx_size:
        used_tokens = exact_tokens
        used_pct = used_tokens / ctx_size * 100
    else:
        used_pct = num(ctx.get("used_percentage"), 0, float)
        used_tokens = int(used_pct / 100 * ctx_size)
    session_cost = num(sub(data, "cost").get("total_cost_usd"), 0.0, float)
    session_id = text(data.get("session_id"))
    model_id = text(sub(data, "model").get("id"))
    model_name = text(sub(data, "model").get("display_name")).split("(")[0].strip()
    effort_level = text(sub(data, "effort").get("level"))
    cwd = text(sub(data, "workspace").get("current_dir"), os.getcwd())

    cfg = load_config()
    us_residency = bool(cfg.get("us_residency", False))

    # Locate the session logs from the transcript path Claude Code hands us.
    # Deriving the projects dir from it keeps a statusline pointed at the
    # account it is actually running under, even when several config
    # directories exist and CLAUDE_CONFIG_DIR isn't set in this subprocess.
    raw_transcript = text(data.get("transcript_path"))
    transcript = Path(raw_transcript) if raw_transcript else None
    # Layout is <projects>/<slugified-cwd>/<session>.jsonl, so the projects
    # root is two levels up. Only trust that if it resolves to a real
    # directory — otherwise fall back to the conventional location.
    if transcript is not None and transcript.parent.parent.is_dir():
        projects_dir = transcript.parent.parent
    else:
        projects_dir = CLAUDE_DIR / "projects"
        if transcript is None:
            transcript = transcript_for(session_id, projects_dir, cwd)

    # No payload occupancy means Claude Code didn't recognise the model, not
    # that the context is empty — recover the figure from the transcript so the
    # bar reflects real usage instead of sitting at zero all session.
    if not exact_tokens:
        recovered = last_context_tokens(transcript)
        if recovered:
            used_tokens = recovered
            used_pct = used_tokens / ctx_size * 100 if ctx_size else 0.0

    # Today's cost, from a configurable reset hour
    day_start_hour = int(cfg.get("day_start_hour", 0))
    now = datetime.now()
    day_cutoff = now.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    if day_cutoff > now:
        day_cutoff -= timedelta(days=1)
    daily_cost = cumulative_cost(day_cutoff, projects_dir, us_residency)
    turns = session_turns(transcript)

    # Longer cumulative window alongside it
    window = cfg.get("cost_window", "month")
    cumul_cost = 0.0
    if window:
        win_cutoff = _cost_cutoff(window, cfg)
        if win_cutoff:
            cumul_cost = cumulative_cost(win_cutoff, projects_dir, us_residency)

    dot = f" {fg(*C.OVERLAY0)}•{RESET} "
    gap = "  "

    # ── Line 1 ────────────────────────────────────────────────────────────────
    left = []

    if show(cfg, "update_needed"):
        indicator = update_indicator(model_id)
        if indicator:
            left.append(indicator)

    # Context block — color and bar shape adapt to the context window size
    if show(cfg, "context"):
        per_block = cfg.get("per_block_colors", True)
        if ctx_size > WORKING_ZONE_TOKENS:
            w_pct = min(100.0, used_tokens / WORKING_ZONE_TOKENS * 100)
            ctx_col = working_zone_color(w_pct)
            bar_s = context_bar_split(used_tokens, ctx_size, per_block=per_block)
        else:
            ctx_col = linear_ctx_color(used_pct)
            bar_s = context_bar_linear(used_pct, per_block=per_block)
        pct_s = f"{fg(*ctx_col)}{used_pct:.0f}%{RESET}"
        tokens_s = (
            f"{fg(*C.OVERLAY2)}{fmt_tokens(used_tokens)}"
            f"{fg(*C.SURFACE2)}/{RESET}"
            f"{fg(*C.OVERLAY1)}{fmt_tokens(ctx_size)}{RESET}"
        )
        left.append(f"{pct_s} {bar_s} {tokens_s}")

    # Turn count + session cost
    show_turns = show(cfg, "turns")
    show_sess_cost = show(cfg, "session_cost")
    if show_turns or show_sess_cost:
        turns_s = (
            f"{fg(*turns_color(turns))}{turns}t{RESET}" if (show_turns and turns) else ""
        )
        # Hidden at zero — some plans don't report a session cost at all, and a
        # permanent $0.0 next to real daily totals reads as broken.
        sess_s = (
            fmt_cost(
                session_cost, scale=cfg.get("session_cost_scale", SESSION_COST_SCALE)
            )
            if (show_sess_cost and session_cost)
            else ""
        )
        combined = " ".join(p for p in (turns_s, sess_s) if p)
        if combined:
            left.append(combined)

    if show(cfg, "usage"):
        usage_s = render_usage_block(data, cfg, dot)
        if usage_s:
            left.append(usage_s)

    right = []
    if model_name and show(cfg, "model"):
        model_str = f"{fg(*C.OVERLAY2)}{model_name}{RESET}"
        if effort_level and show(cfg, "effort"):
            model_str += f" {fg(*C.OVERLAY0)}{effort_level}{RESET}"
        right.append(model_str)
    instance_label = text(cfg.get("instance_label"))
    if instance_label and show(cfg, "instance_label"):
        right.append(f"{fg(*C.CLAUDE_ORANGE)}{instance_label}{RESET}")
    if show(cfg, "timestamp"):
        last_dt = last_assistant_time(transcript)
        if last_dt:
            ts_s = last_dt.strftime("%H:%M")
            right.append(f"{fg(*C.SAPPHIRE)}◷{RESET} {fg(*C.OVERLAY2)}{ts_s}{RESET}")

    line1 = gap + gap.join(left)
    if right:
        line1 += dot + gap.join(right)

    # ── Line 2 ────────────────────────────────────────────────────────────────
    # Path cluster joined by `gap`; cost cluster joined by `dot`; the two
    # clusters joined by `dot`.
    path_parts: list[str] = []
    # A worktree directory is named after its branch, so showing both spends
    # most of line 2 saying the same thing twice.
    in_worktree = bool(_worktree_name(data))
    if show(cfg, "cwd") and not in_worktree:
        abbrev = shorten_path(
            abbreviate(cwd), num(cfg.get("cwd_max_length"), CWD_MAX_LENGTH)
        )
        path_parts.append(osc8_link(f"file://{cwd}", f"{fg(*C.BLUE)}{abbrev}{RESET}"))
    if show(cfg, "branch"):
        branch = git_branch(cwd)
    else:
        branch = None
    if branch:
        label = clip(
            branch, num(cfg.get("branch_max_length"), BRANCH_MAX_LENGTH), middle=True
        )
        path_parts.append(f"{fg(*C.SAPPHIRE)}⎇ {label}{RESET}")

    if show(cfg, "worktree"):
        # Standing alone it fills the slot cwd would have had, so it gets
        # cwd's cap rather than the tighter one meant for a parenthetical.
        standalone = not path_parts
        limit = (
            num(cfg.get("cwd_max_length"), CWD_MAX_LENGTH)
            if standalone
            else num(cfg.get("worktree_max_length"), WORKTREE_MAX_LENGTH)
        )
        wt = worktree_block(data, branch, limit, standalone=standalone)
        if wt:
            path_parts.append(wt)

    if show(cfg, "pr"):
        pr = pr_block(data)
        if pr:
            path_parts.append(pr)

    cost_parts: list[str] = []
    cumul_scale = cfg.get("cumulative_cost_scale", CUMULATIVE_COST_SCALE)
    if show(cfg, "daily_cost"):
        cost_parts.append(
            f"{fg(*C.OVERLAY1)}Σ{RESET} {fmt_cost(daily_cost, scale=cumul_scale)}"
            f"{fg(*C.SURFACE2)}·day{RESET}"
        )
    if window and show(cfg, "window_cost"):
        if window == "billing":
            win_label = datetime.now().strftime("%b")
        else:
            win_label = {"week": "wk", "month": "mo"}.get(window, window)
        cost_parts.append(
            f"{fg(*C.OVERLAY1)}Σ{RESET} {fmt_cost(cumul_cost, scale=cumul_scale)}"
            f"{fg(*C.SURFACE2)}·{win_label}{RESET}"
        )

    line2_clusters = []
    if path_parts:
        line2_clusters.append(gap.join(path_parts))
    if cost_parts:
        line2_clusters.append(dot.join(cost_parts))
    line2 = gap + dot.join(line2_clusters) if line2_clusters else ""

    print(line1)
    print(line2)


if __name__ == "__main__":
    main()
