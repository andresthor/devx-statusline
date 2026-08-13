"""JSONL-based cost calculation for Claude Code sessions.

Reads per-message token usage from Claude Code's session JSONL logs and
calculates cost using per-model pricing. Streaming frames of one message
(same ID + request ID within a file) are collapsed to the final frame, but
the same message replayed across files (subagent context) is billed again,
so it is summed. Uses mtime-based caching so only modified files are
re-parsed on each render.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_default_projects_dir = Path.home() / ".claude" / "projects"

# ── Pricing ──────────────────────────────────────────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
#
# Base (input, output) USD per MILLION tokens, by model prefix. Cache rates are
# *derived* from the base input rate (one number to maintain per model):
#   cache read = 0.10x  ·  5-min write = 1.25x  ·  1-hour write = 2.0x  (of base input)
# Claude Code writes almost exclusively to the 1-hour cache tier.

# Corti models are priced separately, from Corti's own model sheet rather than
# the Anthropic page above. Their cached rate is 0.10x base input, which the
# shared multiplier below already gets right. corti-s1-tiny is deliberately
# absent: it is unpriced upstream, so it should surface the warning.
_BASE_PRICING: dict[str, tuple[float, float]] = {
    "corti-s1-ultra-instant-beta": (4.0, 16.0),
    "corti-s1-ultra-instant": (4.0, 16.0),
    "corti-s1-ultra-beta": (4.0, 16.0),
    "corti-s1-ultra": (4.0, 16.0),
    "corti-s1-mini-instant": (1.0, 4.0),
    "corti-s1-mini": (1.0, 4.0),
    "corti-s1-instant": (2.0, 8.0),
    "corti-s1-beta": (2.0, 8.0),
    "corti-s1": (2.0, 8.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    # Intro rate through 2026-08-31 ($2/$10); reverts to $3/$15 after — revisit then.
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Fallback for unknown models — current Opus base. The ⚠ indicator fires when
# this path is taken, so the guess is visible rather than silent.
_FALLBACK_BASE = _BASE_PRICING["claude-opus-5"]

# Fast mode (Opus `/fast`, usage.speed == "fast") — premium (input, output) per
# million; caching multipliers stack on top of these. Models absent here have no
# fast tier and bill at standard base.
_FAST_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (10.0, 50.0),
    "claude-opus-4-8": (10.0, 50.0),
    # 4.7 fast mode was withdrawn; kept so older sessions still price correctly.
    "claude-opus-4-7": (30.0, 150.0),
}

# Cache rates as a multiple of the (possibly fast-adjusted) base input rate.
_CACHE_READ_MULT = 0.10  # cache hit / refresh
_CACHE_5M_MULT = 1.25  # 5-minute cache write
_CACHE_1H_MULT = 2.00  # 1-hour cache write (Claude Code's default tier)

# US data residency applies a 1.1x multiplier to every token category. It is set
# per-request via inference_geo == "us" OR as a workspace default (in which case
# the JSONL carries no "us" marker — see the us_residency arg). Only Opus 4.6 /
# Sonnet 4.6 and later are eligible; earlier models reject the parameter.
_US_RESIDENCY_MULT = 1.1
_RESIDENCY_ELIGIBLE = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def _residency_eligible(model_id: str) -> bool:
    return any(model_id.startswith(p) for p in _RESIDENCY_ELIGIBLE)


# ── Maintenance watchlist ────────────────────────────────────────────────────
# Dates where this pricing table needs a human look — promo windows ending,
# deprecations landing, etc. Surfaced by the statusline's ⚠ indicator once today
# is within WATCH_LEAD_DAYS of (or past) the date.
#
# (ISO date, label explaining what changes)
WATCH_DATES: list[tuple[str, str]] = [
    ("2026-08-31", "Sonnet 5 intro pricing ends → reverts to $3/$15"),
]

WATCH_LEAD_DAYS = 14


def known_model_prefixes() -> tuple[str, ...]:
    """Model-ID prefixes this pricing table recognizes."""
    return tuple(_BASE_PRICING.keys())


def is_known_model(model_id: str) -> bool:
    """Whether ``model_id`` matches a pricing table entry (vs. falling back)."""
    if not model_id:
        return True  # nothing to flag when we don't know the model yet
    return _prefix_lookup(_BASE_PRICING, model_id) is not None


def due_watch_dates(
    today: datetime | None = None, lead_days: int = WATCH_LEAD_DAYS
) -> list[tuple[str, str]]:
    """Watchlist entries that are overdue or within ``lead_days`` of arriving."""
    now = (today or datetime.now()).date()
    due = []
    for iso, label in WATCH_DATES:
        d = datetime.fromisoformat(iso).date()
        if (d - now).days <= lead_days:
            due.append((iso, label))
    return due


# What may follow a table key and still be the same model: a date/version stamp
# (-20251001) or a context-variant tag ([1m]). A bare word suffix is a different
# model — corti-s1-ultra is not corti-s1 — and must miss so the warning fires.
_SAME_MODEL_SUFFIX = re.compile(r"^(-\d|\[)")


def _prefix_lookup(table: dict[str, tuple[float, float]], model_id: str):
    for prefix, rates in table.items():
        if model_id == prefix:
            return rates
        if model_id.startswith(prefix) and _SAME_MODEL_SUFFIX.match(model_id[len(prefix) :]):
            return rates
    return None


def _message_cost(usage: dict, model_id: str, us_residency: bool = False) -> float:
    """USD cost for one message's usage block, in dollars."""
    base_in, base_out = _prefix_lookup(_BASE_PRICING, model_id) or _FALLBACK_BASE

    # Fast mode replaces the base rates; caching/residency stack on top of them.
    if usage.get("speed") == "fast":
        fast = _prefix_lookup(_FAST_PRICING, model_id)
        if fast:
            base_in, base_out = fast

    micros = (
        usage.get("input_tokens", 0) * base_in
        + usage.get("output_tokens", 0) * base_out
        + usage.get("cache_read_input_tokens", 0) * base_in * _CACHE_READ_MULT
    )

    # Cache writes: split 1h/5m from the nested object. Older logs without the
    # split bill at the 1h rate, which is what Claude Code uses in practice.
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        micros += nested.get("ephemeral_1h_input_tokens", 0) * base_in * _CACHE_1H_MULT
        micros += nested.get("ephemeral_5m_input_tokens", 0) * base_in * _CACHE_5M_MULT
    else:
        micros += (
            usage.get("cache_creation_input_tokens", 0) * base_in * _CACHE_1H_MULT
        )

    cost = micros / 1_000_000
    # Surcharge fires on an explicit per-message "us" marker, or on the
    # account-level flag for eligible models when the workspace defaults to US.
    if usage.get("inference_geo") == "us" or (
        us_residency and _residency_eligible(model_id)
    ):
        cost *= _US_RESIDENCY_MULT
    return cost


# ── Per-file cache ───────────────────────────────────────────────────────────
# Key: (file path, us_residency) → (mtime_ns, list of (timestamp, key, cost)).
# Entries are stored unfiltered so the cache works across different cutoffs.
#
# Dedup is PER FILE, not global. Within a file the same message streams as
# several rows under one msg_id:requestId — we keep the richest (max output).
# But the SAME key appearing in different files is a subagent replaying parent
# context, which Anthropic bills again, so those are summed, not collapsed.

# (timestamp, dedup_key, cost)
_Entry = tuple[str, str, float]
_file_cache: dict[tuple[str, bool], tuple[int, list[_Entry]]] = {}


def _parse_jsonl(path: Path, us_residency: bool) -> list[_Entry]:
    """Parse a JSONL file, collapsing streaming partials within the file."""
    # key -> (ts, output_tokens, cost); keep the row with the most output, the
    # final/complete frame of a streamed response (intermediates are partial).
    best: dict[str, tuple[str, int, float]] = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not usage:
                    continue
                ts = d.get("timestamp", "")
                msg_id = msg.get("id", "")
                req_id = d.get("requestId", "")
                key = f"{msg_id}:{req_id}"
                out = usage.get("output_tokens", 0)
                prev = best.get(key)
                if prev is None or out > prev[1]:
                    cost = _message_cost(usage, msg.get("model", ""), us_residency)
                    best[key] = (ts, out, cost)
    except OSError:
        pass
    return [(ts, key, cost) for key, (ts, _out, cost) in best.items()]


def _get_file_entries(path: Path, mtime_ns: int, us_residency: bool) -> list[_Entry]:
    """Get entries for a file, cached by mtime."""
    cache_key = (str(path), us_residency)
    cached = _file_cache.get(cache_key)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    entries = _parse_jsonl(path, us_residency)
    _file_cache[cache_key] = (mtime_ns, entries)
    return entries


# ── Public API ───────────────────────────────────────────────────────────────


def cumulative_cost(
    cutoff: datetime,
    projects_dir: Path | None = None,
    us_residency: bool = False,
) -> float:
    """Calculate total cost across all sessions since cutoff.

    Scans Claude Code JSONL logs, collapses each message's streaming frames
    within its file, and sums costs across files (subagent context replays are
    billed again, so they are not deduplicated across files). Uses mtime-based
    per-file caching — only re-parses changed files. Pass ``us_residency=True``
    when the workspace defaults to US inference (a 1.1x surcharge on eligible
    models that the JSONL does not otherwise record).
    """
    projects_dir = projects_dir or _default_projects_dir
    if not projects_dir.exists():
        return 0.0

    cutoff_ts = cutoff.timestamp()
    # JSONL timestamps are UTC with Z suffix — convert cutoff to match
    cutoff_iso = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    total = 0.0

    for path in projects_dir.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff_ts:
            continue

        entries = _get_file_entries(path, stat.st_mtime_ns, us_residency)
        for ts, _key, cost in entries:
            if ts >= cutoff_iso:
                total += cost

    return total


# ── Turn counting ───────────────────────────────────────────────────────────

_turns_cache: dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, count)


def transcript_for(session_id: str, projects_dir: Path, cwd: str) -> Path:
    """Reconstruct a transcript path from a slugified cwd.

    Only a fallback — prefer the ``transcript_path`` the statusline payload
    provides, which is authoritative and points at the right config directory.
    """
    slug = cwd.replace("/", "-").replace(".", "-")
    return projects_dir / slug / f"{session_id}.jsonl"


def session_turns(path: Path | None) -> int:
    """Count human turns on the active conversation path.

    Follows the parentUuid chain backwards from the last entry to find only
    messages on the current branch (ignoring rewound/abandoned branches).
    A turn is a ``type: "user"`` entry whose ``message.content`` is a plain
    string (real human input), excluding tool results and meta injections.
    """
    if not path:
        return 0
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return 0
    cache_key = str(path)
    cached = _turns_cache.get(cache_key)
    if cached and cached[0] == mtime_ns:
        return cached[1]

    # First pass: index all entries by uuid
    entries: dict[str, dict] = {}  # uuid -> entry
    last_uuid = ""
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                uuid = d.get("uuid")
                if uuid:
                    entries[uuid] = d
                    last_uuid = uuid
    except OSError:
        pass

    # Walk parentUuid chain to find active path
    active: set[str] = set()
    cursor = last_uuid
    while cursor:
        active.add(cursor)
        cursor = entries[cursor].get("parentUuid", "") if cursor in entries else ""

    # Count human turns on the active path
    count = 0
    for uuid in active:
        d = entries[uuid]
        if d.get("type") != "user":
            continue
        msg = d.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            count += 1

    _turns_cache[cache_key] = (mtime_ns, count)
    return count


_context_cache: dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, tokens)


def last_context_tokens(path: Path | None) -> int:
    """Context occupancy taken from the newest assistant message.

    Claude Code omits the ``context_window`` payload block for models it does
    not recognise — anything reached through a proxy, say — which leaves no
    occupancy to draw the bar from. The transcript still records per-message
    usage, and the three input categories sum to the whole prompt, so the
    newest assistant entry carries the figure the payload would have. Returns
    0 when the transcript is missing or holds no usage.
    """
    if not path:
        return 0
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return 0
    cache_key = str(path)
    cached = _context_cache.get(cache_key)
    if cached and cached[0] == mtime_ns:
        return cached[1]

    tokens = 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # Subagents run their own context; theirs is not this thread's.
                if d.get("type") != "assistant" or d.get("isSidechain"):
                    continue
                usage = (d.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                total = sum(
                    usage.get(k) or 0
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                if total:
                    tokens = total
    except OSError:
        return 0

    _context_cache[cache_key] = (mtime_ns, tokens)
    return tokens


def last_assistant_time(path: Path | None) -> datetime | None:
    """Local-time completion of the most recent assistant message.

    Returns the ``timestamp`` of the last ``type: "assistant"`` entry in the
    active session transcript — when Claude last returned a message — converted
    from its UTC ISO8601 form to local time. ``None`` if the transcript is
    missing or holds no assistant entry.
    """
    if not path:
        return None
    last_ts = ""
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("type") != "assistant":
                    continue
                ts = d.get("timestamp", "")
                if ts:
                    last_ts = ts
    except OSError:
        return None
    if not last_ts:
        return None
    try:
        dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone()
