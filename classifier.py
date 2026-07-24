"""Classify Telegram attendance messages into one or more signals.

Routing:
  * A message that is ONLY +/- tokens (optionally +1/-1 or a (HH:MM) that we now
    ignore) is handled by the fast rules.
  * Anything with extra content (reasons, dates, names, free text) goes to the LLM.
  * Obvious greetings / small talk are ignored (not stored).

`-` semantics (stateful, per worker):
  * a `-` that closes an open `+` within 16h  -> clock_out
  * a `-` with no open `+` in 16h (or after another `-`) -> day_off

Times are not stored; `+(10:30)` just means "present that day".
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import config

INTENTS = ("clock_in", "clock_out", "sick", "vacation", "public_holiday", "day_off", "unresolved", "other")
IGNORE = "ignore"          # casual / greeting → skip, do not store
_VALID = set(INTENTS) | {IGNORE}


@dataclass
class Signal:
    intent: str
    confidence: float
    source: str                      # rule | llm | none
    text: str = ""
    dates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- normalization ---------------------------------------------------------

_DASHES = {"–": "-", "—": "-", "−": "-", "‑": "-"}


def _norm(s: str) -> str:
    for a, b in _DASHES.items():
        s = s.replace(a, b)
    return s.replace("＋", "+")


# --- casual / greeting pre-filter (multilingual) ---------------------------
# No reliable pip library exists for multilingual greeting detection, so this is
# a compact curated set; the LLM (which handles every language) is the backstop
# via the "ignore" intent for anything longer that slips through.
_CASUAL = {
    # en
    "hi", "hii", "hello", "hey", "heya", "yo", "welcome", "thanks", "thank", "thx",
    "ty", "cheers", "congrats", "congratulations", "gm", "gn", "morning", "ok",
    "okay", "np", "please", "pls", "sorry", "lol", "nice", "great", "good", "bye",
    # ru / uk
    "привет", "приветик", "здравствуй", "здравствуйте", "привіт", "вітаю", "спасибо",
    "спс", "дякую", "поздравляю", "вітаю", "доброе", "доброго", "утро", "утра",
    "день", "вечер", "добрый", "ок", "окей", "пожалуйста", "welcome",
    # az / tr
    "salam", "salamlar", "sağol", "təşəkkür", "təşəkkürlər", "merhaba", "teşekkür",
    "təbrik", "xoş", "gəlmisən", "gəlmisiniz",
    # it
    "ciao", "grazie", "benvenuto", "buongiorno", "salve", "prego",
    # ro
    "bună", "salut", "mulțumesc", "mult", "bine", "venit", "felicitări",
}

_word_re = re.compile(r"[^\W\d_]+", re.UNICODE)  # unicode letters only


def is_casual(text: str) -> bool:
    """True for short greetings / pleasantries (emoji-only, or all words casual)."""
    words = _word_re.findall(text.lower())
    if not words:
        return True  # emoji / punctuation / numbers only
    if len(words) > 6:
        return False  # long messages: let the LLM decide
    return all(w in _CASUAL for w in words)


# --- pure +/- detection ----------------------------------------------------
# A "pure" line: a sign, optional 1 (the +1/-1 idiom), optional (HH:MM)/bare time,
# and nothing else. A message is rule-handled only if EVERY non-empty line is pure.
_PURE_LINE = re.compile(r"^[+-]\s*(?:1)?\s*(?:\(?\s*\d{1,2}(?:[:.]\d{2})?\s*\)?)?\s*$")


# --- stateful `-` resolution (per worker) ----------------------------------
_open_plus: dict = {}         # worker key -> datetime of an unclosed `+`
_last_plus: dict = {}         # worker key -> datetime of the most recent `+` (any)
_OPEN_WINDOW = timedelta(hours=16)


def reset_state() -> None:
    """Clear the per-worker +/- state (used before replaying a fresh timeline)."""
    _open_plus.clear()
    _last_plus.clear()


def dump_state() -> dict:
    """Serialize the per-worker +/- state so it survives a restart — otherwise a
    `-` whose matching `+` was seen before the restart can't pair as a clock_out."""
    return {
        "open": {str(k): v.isoformat() for k, v in _open_plus.items()},
        "last": {str(k): v.isoformat() for k, v in _last_plus.items()},
    }


def load_state(d: dict) -> None:
    """Restore state from dump_state(). Keys are worker ids; values ISO datetimes."""
    _open_plus.clear()
    _last_plus.clear()
    for target, src in (("open", _open_plus), ("last", _last_plus)):
        for k, v in ((d or {}).get(target) or {}).items():
            try:
                src[int(k)] = datetime.fromisoformat(v)
            except Exception:
                pass


def _register_plus(key, ts: datetime | None) -> None:
    ts = ts or datetime.now(timezone.utc)
    _open_plus[key] = ts
    _last_plus[key] = ts


def _resolve_minus(key, ts: datetime | None) -> tuple[str, float]:
    ts = ts or datetime.now(timezone.utc)
    op = _open_plus.pop(key, None)  # consume any open +
    last = _last_plus.get(key)
    # A `-` closes the day whenever there was a clock-in the SAME day (a same-day
    # `+` always pairs as a clock-out). Otherwise the 16h window handles overnight
    # shifts (a `+` late yesterday closed by a `-` this morning). A `-` with no
    # same-day `+` and no open `+` within 16h is ambiguous -> `unresolved` (an
    # explicit "day off" message is still classified day_off by the LLM instead).
    if last is not None and last.date() == ts.date():
        return "clock_out", 0.95
    if op is not None and (ts - op) <= _OPEN_WINDOW:
        return "clock_out", 0.95
    return "unresolved", 0.9


def parse_rules(text: str, key=None, ts: datetime | None = None) -> list[Signal] | None:
    """Return signals if the whole message is pure +/-, else None (→ LLM)."""
    lines = [l.strip() for l in _norm(text).splitlines()]
    lines = [l for l in lines if l]
    if not lines or not all(_PURE_LINE.match(l) for l in lines):
        return None
    out: list[Signal] = []
    for line in lines:
        if line[0] == "+":
            _register_plus(key, ts)
            out.append(Signal("clock_in", 0.97, "rule", line))
        else:
            intent, conf = _resolve_minus(key, ts)
            out.append(Signal(intent, conf, "rule", line))
    return out


# --- LLM fallback (OpenAI-compatible) --------------------------------------

_SYSTEM = (
    "You classify a short work-chat attendance message. "
    "Respond ONLY with a JSON object of the form "
    '{"intent": "...", "confidence": 0.0, "dates": []}. '
    "intent is one of: clock_in, clock_out, sick, vacation, public_holiday, "
    "day_off, ignore, other. Use 'ignore' for greetings / small talk / anything "
    "not about attendance. In dates, list EVERY individual calendar day the "
    "message covers, as ISO YYYY-MM-DD. For a single day give one date; for a "
    "range (e.g. '12-16.01' or '12.01-16.01') list each day in it "
    "(2025-01-12, 2025-01-13, 2025-01-14, 2025-01-15, 2025-01-16). Resolve "
    "partial dates (day.month) against the message date supplied below; never "
    "guess today. A bare + or - with NO date attached refers to the message's "
    "own day — include that day in dates. So a message clocking in for today AND "
    "for a past day (e.g. '+ and + for yesterday 01.07') must list BOTH the "
    "message day and that past day. Leave dates empty only when the message "
    "names no day at all. confidence is 0-1."
)

_client = None
_AI_MAX_CHARS = 500

# Model failover: try the primary model, then the fallback, on the SAME endpoint.
# `_pref_idx` remembers the last model that worked so a dead primary isn't retried
# on every message; it's periodically reset so the primary is re-checked.
_pref_idx = 0
_pref_ts = 0.0
_PREF_RETRY = 300.0   # re-prefer the primary this many seconds after a failover


def _models() -> list:
    """[primary, fallback] with blanks/dupes removed."""
    out = []
    for m in (config.OPENAI_MODEL, config.OPENAI_FALLBACK_MODEL):
        if m and m not in out:
            out.append(m)
    return out


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        kwargs = {"api_key": config.OPENAI_API_KEY}
        if config.OPENAI_BASE_URL:
            kwargs["base_url"] = config.OPENAI_BASE_URL
        _client = OpenAI(**kwargs)
    return _client


def _chat(messages, **kw):
    """One chat completion with primary→fallback model failover. Starts from the
    last-known-good model, falls through the rest, and remembers which one worked.
    Raises the last error only if EVERY model fails (i.e. the endpoint is down)."""
    global _pref_idx, _pref_ts
    models = _models()
    if not models:
        raise RuntimeError("no OpenAI model configured")
    now = time.monotonic()
    if _pref_idx and now - _pref_ts >= _PREF_RETRY:
        _pref_idx = 0                      # periodically re-check the primary
    order = list(range(_pref_idx, len(models))) + list(range(0, _pref_idx))
    client = _get_client()
    last = None
    for idx in order:
        try:
            resp = client.chat.completions.create(model=models[idx], messages=messages, **kw)
            if idx != _pref_idx:
                if idx:
                    print(f"[classifier] model failover → {models[idx]!r} (primary unavailable)")
                _pref_idx, _pref_ts = idx, now
            return resp
        except Exception as exc:
            last = exc
    raise last


def ai_healthcheck() -> bool:
    """True if the endpoint answers on the primary OR the fallback model. Used to
    gate processing when AI-assist is on (so nothing is misclassified while down)."""
    if not config.OPENAI_API_KEY:
        return False
    try:
        _chat([{"role": "user", "content": "ok"}], max_tokens=1, temperature=0, timeout=15)
        return True
    except Exception:
        return False


def classify_llm(text: str, ts: datetime | None = None) -> Signal:
    if not config.OPENAI_API_KEY:
        return Signal("other", 0.0, "none", text)
    try:
        import json

        sent_on = f"\n\nThe message was sent on {ts.date().isoformat()} — resolve partial/relative dates against this." if ts else ""
        prompt = f'{_SYSTEM}{sent_on}\n\nMessage to classify: {text[:_AI_MAX_CHARS]!r}\n\nOutput ONLY the JSON object, no other words.'
        resp = _chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=200)
        content = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0)) if m else {}
        intent = data.get("intent") if data.get("intent") in _VALID else "other"
        conf = float(data.get("confidence", 0.5))
        return Signal(intent, max(0.0, min(1.0, conf)), "llm", text, list(data.get("dates") or []))
    except Exception as exc:
        print(f"[classifier] LLM error: {exc!r}")
        return Signal("other", 0.0, "none", text)


def classify(text: str, ai: bool = False, key=None, ts: datetime | None = None) -> list[Signal]:
    """Rules for pure +/- messages; the LLM for everything else; casual → ignore."""
    if not text or not text.strip():
        return [Signal(IGNORE, 1.0, "rule", text or "")]

    # Pure +/- messages first (a lone '+' has no letters and would otherwise look
    # "casual" to is_casual).
    ruled = parse_rules(text, key=key, ts=ts)
    if ruled is not None:
        return ruled

    if is_casual(text):
        return [Signal(IGNORE, 0.95, "rule", text)]

    # anything else → LLM (if enabled), else parked as 'other'
    if ai and config.OPENAI_API_KEY:
        return [classify_llm(text, ts=ts)]
    return [Signal("other", 0.0, "none", text)]


def expand_dates(sig: Signal, fallback_date: str | None) -> list:
    """One entry per calendar day to store for this signal. A signal carrying an
    explicit date list (e.g. an LLM-parsed vacation range) becomes one row per
    day; otherwise it falls back to the message's own date — a single row."""
    days = [d for d in (sig.dates or []) if d]
    return days or [fallback_date]
