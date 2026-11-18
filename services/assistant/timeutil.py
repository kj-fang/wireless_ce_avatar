"""
Time handling shared by the connectors.

Everything the user says about time ("today", "since yesterday", "2 pm") is
about the clock of THEIR PC. Salesforce stores UTC and its own TODAY literal
follows the Salesforce user's time-zone setting, so every boundary is worked
out here in local time and sent out as an absolute UTC instant.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

MAX_WINDOW_DAYS = 31


def as_local(dt: datetime, now: datetime) -> datetime:
    """`dt` in the time zone of `now` (a naive value is taken as local)."""
    tz = now.tzinfo or timezone.utc
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def window_start(value: Optional[str], now: datetime) -> datetime:
    """The instant a "since ..." window starts, as an aware local datetime.

        today (default)      local midnight
        yesterday            local midnight, one day back
        24h, 36 hours        that many hours before now
        7d, 3 days           that many days back, from local midnight
        2026-11-17           that day's local midnight
        2026-11-17T14:00     that local time (an offset in the value is honoured)

    Raises ValueError for anything else, or a start in the future. A start
    more than MAX_WINDOW_DAYS back is pulled up to that limit."""
    text = str(value or "today").strip().lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text in ("", "today", "今天", "今日"):
        start = midnight
    elif text in ("yesterday", "昨天", "昨日"):
        start = midnight - timedelta(days=1)
    elif m := re.fullmatch(r"(\d{1,3})\s*(h|hr|hrs|hour|hours)", text):
        start = now - timedelta(hours=int(m.group(1)))
    elif m := re.fullmatch(r"(\d{1,3})\s*(d|day|days)", text):
        start = midnight - timedelta(days=int(m.group(1)))
    elif text in ("this week", "week"):
        start = midnight - timedelta(days=now.weekday())
    else:
        try:
            start = as_local(datetime.fromisoformat(text.replace("z", "+00:00").upper()
                                                    if "t" in text else text), now)
        except ValueError:
            raise ValueError(
                f"cannot read {value!r} as a start time: use today, yesterday, 24h, 7d, "
                "or an ISO date / date-time") from None
    if start > now:
        raise ValueError(f"{value!r} is in the future")
    return max(start, now - timedelta(days=MAX_WINDOW_DAYS))


def soql_utc(dt: datetime) -> str:
    """An aware datetime as a SOQL datetime literal (UTC, to the second)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value) -> Optional[datetime]:
    """An aware datetime from the timestamp shapes the app meets:
    Salesforce's '2026-11-18T03:12:45.000+0000', ISO strings with or without
    an offset (without: local time), or None when unreadable."""
    text = str(value or "").strip()
    if not text:
        return None
    if re.search(r"[+-]\d{4}$", text):                      # +0000 -> +00:00
        text = text[:-2] + ":" + text[-2:]
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def label(value, now: datetime) -> str:
    """A timestamp as the user reads it: 'HH:MM' for today, 'yesterday HH:MM',
    else 'MM-DD HH:MM' - all in the PC's own time zone. "" when unreadable."""
    dt = value if isinstance(value, datetime) else parse_stamp(value)
    if dt is None:
        return ""
    dt = as_local(dt, now)
    days = (now.date() - dt.date()).days
    if days == 0:
        return dt.strftime("%H:%M")
    if days == 1:
        return dt.strftime("yesterday %H:%M")
    return dt.strftime("%m-%d %H:%M") if dt.year == now.year else dt.strftime("%Y-%m-%d %H:%M")


def stamp(dt: datetime) -> str:
    """'2026-11-18 09:30 (+08:00)' - an instant with its offset spelled out."""
    off = dt.strftime("%z")
    off = f"{off[:3]}:{off[3:]}" if len(off) == 5 else off
    return f"{dt.strftime('%Y-%m-%d %H:%M')} ({off})"
