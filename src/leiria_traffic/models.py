"""Value objects shared by providers, the engine and the API."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

SLOT_MINUTES = 10
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES  # 144


class Source(str, Enum):
    """Where a number came from. Shown to the user next to every value."""

    PROVIDER_PREDICTION = "provider_prediction"  # the routing provider's own model
    MEASURED_STATS = "measured_stats"  # our percentiles over collected observations
    WEATHER_ADJUSTMENT = "weather_adjustment"  # our optional rain factor
    CALENDAR_ADJUSTMENT = "calendar_adjustment"  # reserved: needs measured evidence first


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    @classmethod
    def parse(cls, text: str) -> "Point":
        lat, lon = (float(v) for v in text.split(","))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"coordinates out of range: {text}")
        return cls(lat, lon)

    def key(self) -> str:
        # 5 decimals ≈ 1 m: stable cache key, avoids float noise
        return f"{self.lat:.5f},{self.lon:.5f}"


@dataclass(frozen=True)
class RouteEstimate:
    """One provider answer for one departure time."""

    provider: str
    departure: datetime
    duration_s: int  # the provider's best estimate for this departure
    distance_m: int | None = None
    typical_s: int | None = None  # historical-pattern estimate, if exposed separately
    free_flow_s: int | None = None  # no-traffic estimate, if exposed
    live_s: int | None = None  # live-traffic estimate, if exposed
    low_s: int | None = None  # provider optimistic bound (Google only)
    high_s: int | None = None  # provider pessimistic bound (Google only)
    cacheable: bool = True  # False when the provider's terms forbid storing results

    @property
    def arrival(self) -> datetime:
        return self.departure + timedelta(seconds=self.duration_s)

    @property
    def delay_s(self) -> int | None:
        return None if self.free_flow_s is None else self.duration_s - self.free_flow_s


@dataclass(frozen=True)
class Weather:
    precipitation_mm: float | None  # mm in the departure hour
    temperature_c: float | None
    source: str  # "open-meteo-forecast", "open-meteo-archive", "override", "unavailable"

    @property
    def raining(self) -> bool | None:
        return None if self.precipitation_mm is None else self.precipitation_mm >= 0.2

    @property
    def intensity(self) -> str | None:
        p = self.precipitation_mm
        if p is None:
            return None
        if p < 0.2:
            return "dry"
        return "light" if p < 2.5 else "moderate" if p < 7.6 else "heavy"  # AMS rain-rate classes


@dataclass(frozen=True)
class DayContext:
    day: date
    weekday: str
    month: int
    season: str
    weekend: bool
    public_holiday: str | None
    school_term: bool
    school_break: str | None
    events: tuple[str, ...] = ()

    def label(self) -> str:
        parts = [self.weekday]
        if self.public_holiday:
            parts.append(f"holiday ({self.public_holiday})")
        elif self.weekend:
            parts.append("weekend")
        parts.append("school term" if self.school_term else f"school break ({self.school_break or 'none'})")
        parts.append(self.season)
        parts.extend(self.events)
        return " · ".join(parts)


@dataclass
class SlotResult:
    departure: str  # HH:MM
    duration_min: float | None
    arrival: str | None
    source: Source | None
    provider: str | None = None
    typical_min: float | None = None
    free_flow_min: float | None = None
    low_min: float | None = None
    high_min: float | None = None
    distance_km: float | None = None
    weather: str | None = None
    adjustments: list[dict] = field(default_factory=list)
    measured: dict | None = None  # P50/P75/P90… from our own observations
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source"] = self.source.value if self.source else None
        return d


def slot_times(day: date) -> list[time]:
    """00:00, 00:10 … 23:50."""
    return [time(i * SLOT_MINUTES // 60, i * SLOT_MINUTES % 60) for i in range(SLOTS_PER_DAY)]


def snap_to_slot(t: time) -> time:
    """Round down to the 10-minute grid (the cache resolution)."""
    return time(t.hour, t.minute - t.minute % SLOT_MINUTES)
