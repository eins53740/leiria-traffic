"""FastAPI app: JSON API + the single-page UI in static/."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .heatmap import heatmap
from .models import Point
from .routing import ProviderError
from .wiring import App, build

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
state: dict[str, App] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async with httpx.AsyncClient(timeout=20) as client:
        state["app"] = build(Settings.load(), client)
        yield
    state.clear()


api = FastAPI(title="Leiria traffic", lifespan=lifespan)
api.mount("/static", StaticFiles(directory=STATIC), name="static")


def _app() -> App:
    return state["app"]


def _point(text: str, name: str) -> Point:
    try:
        return Point.parse(text)
    except ValueError as e:
        raise HTTPException(422, f"{name}: expected 'lat,lon' ({e})") from None


@api.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@api.get("/api/estimate")
async def estimate(origin: str, dest: str, day: date, at: time,
                   weather: str = Query("auto", pattern="^(auto|dry|rain)$")) -> dict:
    """Travel time for one departure, snapped to the 10-minute grid."""
    return await _app().engine.estimate(_point(origin, "origin"), _point(dest, "dest"), day, at, weather)


@api.get("/api/profile")
async def profile(origin: str, dest: str, day: date,
                  weather: str = Query("auto", pattern="^(auto|dry|rain)$")) -> dict:
    """All 144 ten-minute departures of one day."""
    try:
        return await _app().engine.profile(_point(origin, "origin"), _point(dest, "dest"), day, weather)
    except LookupError as e:
        raise HTTPException(422, str(e)) from None


@api.get("/api/scenarios")
def scenarios(weekday: int = Query(0, ge=0, le=6)) -> list[dict]:
    """Next real date for term / break / summer / holiday on that weekday."""
    return _app().engine.scenario_dates(weekday, date.today())


@api.get("/api/heatmap")
async def get_heatmap(day: date, at: time, mode: str = Query("time", pattern="^(time|ratio)$"),
                      direction: str = Query("in", pattern="^(in|out)$"), per_side: int = Query(10, ge=3, le=10),
                      radius_km: float = Query(6.0, gt=0, le=20)) -> dict:
    a = _app()
    if a.tomtom is None:
        raise HTTPException(503, "heatmap needs TOMTOM_API_KEY (matrix routing)")
    try:
        return await heatmap(a.tomtom, a.store, a.engine.cfg.cache_ttl, day, at, mode=mode,
                             inbound=direction == "in", per_side=per_side, radius_km=radius_km)
    except ProviderError as e:
        raise HTTPException(502, str(e)) from None


@api.get("/api/geocode")
async def geocode(q: str = Query(..., min_length=2)) -> list[dict]:
    """Place search biased to Leiria (TomTom Search API)."""
    a = _app()
    if not a.settings.tomtom_key:
        raise HTTPException(503, "geocoding needs TOMTOM_API_KEY")
    r = await a.client.get(f"https://api.tomtom.com/search/2/search/{quote(q, safe='')}.json", params={
        "key": a.settings.tomtom_key, "countrySet": "PT", "lat": 39.7436, "lon": -8.8071, "limit": 6})
    if r.status_code != 200:
        raise HTTPException(502, f"geocoder HTTP {r.status_code}")
    return [{"label": x["address"].get("freeformAddress", ""), "lat": x["position"]["lat"],
             "lon": x["position"]["lon"]} for x in r.json().get("results", [])]


@api.get("/api/routes")
def routes() -> list[dict]:
    return [dict(r) for r in _app().store.routes()]


@api.post("/api/routes")
def save_route(name: str, origin: str, dest: str, origin_label: str = "", dest_label: str = "",
               collect: bool = False) -> dict:
    rid = _app().store.add_route(name, _point(origin, "origin"), _point(dest, "dest"),
                                 origin_label, dest_label, collect)
    return {"route_id": rid}
