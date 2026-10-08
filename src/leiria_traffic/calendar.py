"""School calendar, public holidays and events -> DayContext."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import holidays
import yaml

from .models import DayContext

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _d(s: str) -> date:
    return date.fromisoformat(str(s))


def season(day: date) -> str:
    """Meteorological season, plus the Portuguese 'summer holiday' window (Jul-Aug)."""
    m = day.month
    if m in (7, 8):
        return "summer holidays"
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}[m]


class SchoolCalendarProvider(Protocol):
    def status(self, day: date) -> tuple[bool, str | None]: ...


class HolidayProvider(Protocol):
    def holiday(self, day: date) -> str | None: ...


class YamlSchoolCalendar:
    """Reads data/school_calendar.yaml. Raises for dates the file does not cover,
    so a stale calendar is visible instead of silently returning 'holiday'."""

    def __init__(self, path: Path):
        self._years = yaml.safe_load(path.read_text(encoding="utf-8"))["years"]

    def covers(self, day: date) -> bool:
        first = min(_d(y["term"][0]) for y in self._years.values())
        last_year = max(self._years)
        last = date(_d(self._years[last_year]["term"][1]).year, 9, 10)  # up to next year's start
        return date(first.year, 7, 1) <= day <= last

    def status(self, day: date) -> tuple[bool, str | None]:
        """(in_term, break_name). break_name is 'summer' outside any term range."""
        for y in self._years.values():
            start, end = _d(y["term"][0]), _d(y["term"][1])
            if start <= day <= end:
                for name, (b0, b1) in y["breaks"].items():
                    if _d(b0) <= day <= _d(b1):
                        return False, name
                return day.weekday() < 5, None if day.weekday() < 5 else "weekend"
        if not self.covers(day):
            raise LookupError(f"school calendar has no data for {day}; update data/school_calendar.yaml")
        return False, "summer"


class PortugalHolidays:
    """National holidays + the municipal holiday of `subdiv` (10 = Leiria district,
    which the `holidays` library maps to Leiria's 22 May). Carnival is OPTIONAL
    in Portugal; it is reported because most services close."""

    def __init__(self, subdiv: str | None = "10"):
        self._subdiv = subdiv
        self._cache: dict[int, holidays.HolidayBase] = {}

    def holiday(self, day: date) -> str | None:
        if day.year not in self._cache:
            self._cache[day.year] = holidays.Portugal(
                years=day.year, subdiv=self._subdiv, categories=("public", "optional"))
        return self._cache[day.year].get(day)


@dataclass
class EventCalendar:
    one_off: list[dict]
    recurring: list[dict]

    @classmethod
    def load(cls, path: Path) -> "EventCalendar":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(data.get("events") or [], data.get("recurring") or [])

    def on(self, day: date) -> tuple[str, ...]:
        names = [e["name"] for e in self.one_off if _d(e["start"]) <= day <= _d(e.get("end", e["start"]))]
        for e in self.recurring:
            if WEEKDAYS[day.weekday()] in e["weekdays"] and _d(e["since"]) <= day and (
                    "until" not in e or day <= _d(e["until"])):
                names.append(e["name"])
        return tuple(names)


class ContextBuilder:
    def __init__(self, school: SchoolCalendarProvider, hols: HolidayProvider, events: EventCalendar):
        self.school, self.hols, self.events = school, hols, events

    def build(self, day: date) -> DayContext:
        holiday = self.hols.holiday(day)
        in_term, brk = self.school.status(day)
        if holiday:
            in_term = False
            brk = brk or "public holiday"
        return DayContext(
            day=day, weekday=WEEKDAY_NAMES[day.weekday()], month=day.month, season=season(day),
            weekend=day.weekday() >= 5, public_holiday=holiday, school_term=in_term,
            school_break=brk, events=self.events.on(day))
