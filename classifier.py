"""Classify Telegram attendance messages into one or more signals.

Grammar learned from real client groups (all English, line-based):
    +              present (clock in)
    +1             present
    +(10:30)       present, arrived 10:30
    -              clock out (default) / absent
    -1             clock out
    -(19:00)       left at 19:00
    - sick / (sick leave) / sick-day / 🤒     -> sick
    - vac / vacation                          -> vacation
    - public holiday                          -> public_holiday
    - day off / time-off                      -> day_off

A single message can carry several signals — a name line plus a +/- line, or
`+(10)` and `-(19)` on separate lines — so classify() returns a LIST of signals
(one row each). Rules cover the common grammar; when AI-assist is enabled and a
rule is unsure, a light model refines it. With AI off, unknown text stays 'other'.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import config

INTENTS = ("clock_in", "clock_out", "sick", "vacation", "public_holiday", "other")


@dataclass
class Signal:
    intent: str
    confidence: float
    source: str                      # rule | llm | none
    text: str = ""                   # the line/segment this came from
    time: str | None = None          # clock time, e.g. "10:30" or "10"
    dates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --- normalization ---------------------------------------------------------

_DASHES = {"–": "-", "—": "-", "−": "-", "‑": "-"}


def _norm(s: str) -> str:
    for a, b in _DASHES.items():
        s = s.replace(a, b)
    return s.replace("＋", "+")


# reason keyword -> intent (substring match, lowercased, checked in order)
_REASON = [
    ("sick", "sick"), ("🤒", "sick"), ("больн", "sick"),
    ("vacation", "vacation"), ("vac", "vacation"), ("отпуск", "vacation"),
    ("public holiday", "public_holiday"), ("holiday", "public_holiday"),
]


def _reason_of(s: str) -> str | None:
    low = s.lower()
    for kw, intent in _REASON:
        if kw in low:
            return intent
    return None


_PAREN_TIME = re.compile(r"^\(\s*(\d{1,2})(?:[:.](\d{2}))?\s*\)")
_BARE_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})\b")


def _extract_time(rest: str) -> tuple[str | None, str]:
    """A parenthesized or colon time at the start of `rest`, e.g. '(10:30)', '(10)',
    '10:30'. A lone '1' (the +1/-1 idiom) is NOT a time."""
    m = _PAREN_TIME.match(rest) or _BARE_TIME.match(rest)
    if m:
        hh, mm = m.group(1), m.group(2)
        t = f"{int(hh)}:{mm}" if mm else hh
        return t, rest[m.end():].strip()
    return None, rest


def _make(sign: str, reason: str, time: str | None, text: str) -> Signal:
    if not reason:
        if sign == "+":
            return Signal("clock_in", 0.97, "rule", text, time)
        return Signal("clock_out", 0.90, "rule", text, time)
    intent = _reason_of(reason)
    if intent:
        return Signal(intent, 0.95, "rule", text, time)
    # +/- with an unrecognized reason: keep the sign's default, low confidence
    # (a candidate for AI refinement when AI-assist is on).
    default = "clock_in" if sign == "+" else "clock_out"
    return Signal(default, 0.60, "rule", text, time)


def parse_rules(text: str) -> list[Signal]:
    """Rule-only parse into zero or more signals."""
    lines = [l.strip() for l in _norm(text).splitlines()]
    lines = [l for l in lines if l]
    out: list[Signal] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line[:1] not in ("+", "-"):
            i += 1
            continue  # name / greeting / chatter line
        sign = line[0]
        rest = line[1:].strip()
        if rest == "1":  # the +1 / -1 idiom
            rest = ""
        time, rest = _extract_time(rest)
        reason = rest.strip(" ()[]:.\t").lower()
        # Bare sign whose reason sits on the NEXT line (e.g. "-\npublic holiday").
        if not reason and i + 1 < len(lines) and lines[i + 1][:1] not in ("+", "-"):
            if _reason_of(lines[i + 1]) is not None:
                reason = lines[i + 1].strip(" ()[]:.\t").lower()
                line = f"{line} {lines[i + 1]}"
                i += 1
        out.append(_make(sign, reason, time, line))
        i += 1
    return out


# --- AI fallback (light model, OpenAI-compatible API) ----------------------

_SYSTEM = (
    "You classify a short work-chat attendance message. "
    "Respond ONLY with a JSON object of the form "
    '{"intent": "...", "confidence": 0.0, "dates": []}. '
    "intent must be one of: clock_in, clock_out, sick, vacation, "
    "public_holiday, other. "
    "Put any explicit dates as ISO strings (YYYY-MM-DD) in dates (empty list if "
    "none; do not guess today). confidence is a calibrated number between 0 and 1."
)

_client = None


def _get_client():
    """Lazily build an OpenAI client (supports a custom base_url for
    OpenAI-compatible / self-hosted endpoints)."""
    global _client
    if _client is None:
        from openai import OpenAI
        kwargs = {"api_key": config.OPENAI_API_KEY}
        if config.OPENAI_BASE_URL:
            kwargs["base_url"] = config.OPENAI_BASE_URL
        _client = OpenAI(**kwargs)
    return _client


def classify_llm(text: str) -> Signal:
    if not config.OPENAI_API_KEY:
        return Signal("other", 0.0, "none", text)
    try:
        import json
        import re

        # Put the instruction in the USER turn — some OpenAI-compatible proxies
        # override or ignore the system role.
        prompt = f'{_SYSTEM}\n\nMessage to classify: {text!r}\n\nOutput ONLY the JSON object, no other words.'
        resp = _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        content = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", content, re.S)  # tolerate stray text around the JSON
        data = json.loads(m.group(0)) if m else {}
        intent = data.get("intent") if data.get("intent") in INTENTS else "other"
        conf = float(data.get("confidence", 0.5))
        dates = data.get("dates") or []
        return Signal(intent, max(0.0, min(1.0, conf)), "llm", text, None, list(dates))
    except Exception as exc:
        print(f"[classifier] LLM error: {exc!r}")
        return Signal("other", 0.0, "none", text)


def classify(text: str, ai: bool = False) -> list[Signal]:
    """Return one or more signals for a message. `ai` enables the light-model
    fallback for messages/segments the rules can't classify confidently."""
    if not text or not text.strip():
        return [Signal("other", 0.0, "none", text or "")]

    use_ai = ai and bool(config.OPENAI_API_KEY)
    signals = parse_rules(text)

    if not signals:  # no +/- anywhere
        if use_ai:
            return [classify_llm(text)]
        return [Signal("other", 0.0, "none", text)]

    if use_ai:
        refined: list[Signal] = []
        for s in signals:
            if s.confidence < 0.9:
                ai_sig = classify_llm(s.text)
                if ai_sig.confidence > s.confidence:
                    ai_sig.time = s.time or ai_sig.time
                    refined.append(ai_sig)
                    continue
            refined.append(s)
        return refined
    return signals
