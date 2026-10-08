"""Settings from environment variables (a repo-root .env is read if present; real env wins)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    tomtom_key: str
    google_key: str
    providers: tuple[str, ...]
    google_bounds: bool
    tomtom_qps: float
    db_path: Path
    cache_ttl_days: int
    rain_adjust_pct: float
    holiday_subdiv: str | None
    data_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        e = os.environ.get
        data = Path(e("LT_DATA_DIR", str(ROOT / "data")))
        return cls(
            tomtom_key=e("TOMTOM_API_KEY", ""),
            google_key=e("GOOGLE_MAPS_API_KEY", ""),
            providers=tuple(p.strip() for p in e("LT_PROVIDERS", "tomtom,google").split(",") if p.strip()),
            google_bounds=e("LT_GOOGLE_BOUNDS", "0") == "1",
            tomtom_qps=float(e("LT_TOMTOM_QPS", "5")),
            db_path=Path(e("LT_DB_PATH", str(data / "traffic.db"))),
            cache_ttl_days=int(e("LT_CACHE_TTL_DAYS", "7")),
            rain_adjust_pct=float(e("LT_RAIN_ADJUST_PCT", "0")),
            holiday_subdiv=e("LT_HOLIDAY_SUBDIV", "10") or None,
            data_dir=data,
        )


def setup_logging() -> None:
    """INFO logs for the app; httpx stays at WARNING because its INFO line prints the
    full request URL, and the TomTom key travels in the query string."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
