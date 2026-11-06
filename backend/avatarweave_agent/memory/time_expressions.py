"""Grounded Chinese clock/part-of-day expressions, without invented durations."""

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .contracts import TimeWindow

DAY = r"(?:今天|明天|后天|昨天|前天|(?:上周|本周|下周)[一二三四五六日天]|\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?)"
PART = r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上)"
NUMBER = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
CLOCK = re.compile(
    rf"(?<![\d零〇一二两三四五六七八九十])(?P<day>{DAY})?\s*"
    rf"(?P<part>{PART})?\s*(?P<hour>{NUMBER})"
    rf"(?:[.:：．](?P<minute>\d{{1,2}})|[点时](?:(?P<quarter>[一三]刻)|(?P<half>半)|(?P<cnminute>{NUMBER})分?|整)?)"
)
PERIOD = re.compile(rf"(?P<day>{DAY})?\s*(?P<part>{PART})")


def number(text):
    if text.isdigit():
        return int(text)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "十" in text:
        left, right = text.split("十")
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return int("".join(str(digits[c]) for c in text))


def date_for(expression, local):
    if not expression:
        return local.date()
    shifts = {"今天": 0, "明天": 1, "后天": 2, "昨天": -1, "前天": -2}
    if expression in shifts:
        return local.date() + timedelta(days=shifts[expression])
    if match := re.fullmatch(r"(上周|本周|下周)([一二三四五六日天])", expression):
        weekday = "一二三四五六日".index(match[2].replace("天", "日"))
        return local.date() + timedelta(
            days=weekday - local.weekday() + {"下周": 7, "上周": -7}.get(match[1], 0)
        )
    match = re.fullmatch(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", expression)
    if match:
        return datetime(*(int(s) for s in match.groups())).date()
    raise ValueError("Unresolved date")


def event_expression(text):
    clocks = list(CLOCK.finditer(text))
    if len(clocks) == 1:
        return clocks[0].group().strip()
    if len(clocks) == 2 and re.fullmatch(
        r"\s*(?:到|至|[-—~～])\s*", text[clocks[0].end() : clocks[1].start()]
    ):
        return text[clocks[0].start() : clocks[1].end()].strip()
    if clocks:
        return None  # Multiple unrelated times require clarification.
    periods = list(PERIOD.finditer(text))
    return periods[0].group().strip() if len(periods) == 1 else None


def meeting_topic(value):
    """Compare explicit meeting topics after removing only generic time/wording."""
    value = CLOCK.sub("", value)
    value = PERIOD.sub("", value)
    value = re.sub(DAY, "", value)
    return re.sub(
        r"请记住|本人|我|会议|开会|有(?:一?个|一?场)?|参加|召开|举行|进行|安排|今天|的|要|在|将|了|[，。！？、,.!?:：;；\s]",
        "",
        value,
    )


def clock_instant(match, local, zone, *, inherited_day=None, inherited_part=None):
    day = match["day"] or inherited_day
    part = match["part"] or inherited_part
    hour = number(match["hour"])
    minute = (
        (15 if match["quarter"] == "一刻" else 45)
        if match["quarter"]
        else 30
        if match["half"]
        else number(match["minute"] or match["cnminute"] or "0")
    )
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Invalid clock")
    if part in {"下午", "傍晚", "晚上"} and 1 <= hour < 12 or part == "中午" and 1 <= hour <= 2:
        hour += 12
    elif part == "凌晨" and hour == 12:
        hour = 0
    elif not part and 1 <= hour <= 12 and not match["hour"].startswith("0"):
        raise ValueError("AM/PM is ambiguous")
    return datetime.combine(date_for(day, local), datetime.min.time(), zone).replace(
        hour=hour, minute=minute
    )


def resolve_clock(expression, anchor, timezone):
    """None means the expression belongs to the existing calendar parser."""
    text = unicodedata.normalize("NFKC", expression).strip()
    zone = ZoneInfo(timezone)
    local = anchor.astimezone(zone)
    clocks = list(CLOCK.finditer(text))
    period = PERIOD.fullmatch(text)
    if not clocks and not period:
        return None
    try:
        if clocks:
            if clocks[0].start() != 0 or clocks[-1].end() != len(text):
                raise ValueError("Unresolved clock suffix")
            start = clock_instant(clocks[0], local, zone)
            if len(clocks) == 1:
                end, precision = start, "instant"
            elif len(clocks) == 2 and re.fullmatch(
                r"\s*(?:到|至|[-—~～])\s*", text[clocks[0].end() : clocks[1].start()]
            ):
                end = clock_instant(
                    clocks[1],
                    local,
                    zone,
                    inherited_day=clocks[0]["day"],
                    inherited_part=clocks[0]["part"],
                )
                if end <= start:
                    raise ValueError("Clock range is reversed")
                precision = "range"
            else:
                raise ValueError("Multiple unrelated clocks")
        else:
            bounds = {
                "凌晨": (0, 6),
                "早上": (6, 12),
                "上午": (6, 12),
                "中午": (12, 14),
                "下午": (12, 18),
                "傍晚": (17, 20),
                "晚上": (18, 24),
            }
            lower, upper = bounds[period["part"]]
            midnight = datetime.combine(date_for(period["day"], local), datetime.min.time(), zone)
            start, end, precision = (
                midnight + timedelta(hours=lower),
                midnight + timedelta(hours=upper),
                "range",
            )
        return (
            TimeWindow(
                start=start.astimezone(UTC),
                end=end.astimezone(UTC),
                precision=precision,
                original_expression=expression,
            ),
            "derived",
            None,
        )
    except (ValueError, KeyError):
        return (
            TimeWindow(precision="unknown", original_expression=expression),
            "ambiguous",
            "请确认具体日期及上午或下午的时间。",
        )
