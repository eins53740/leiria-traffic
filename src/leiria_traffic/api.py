"""FastAPI app: JSON API + the single-page UI in static/."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, time
from pathlib import Path
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, setup_logging
from .heatmap import heatmap
from .models import Point
from .routing import ProviderError
from .wiring import App, build

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
state: dict[str, App] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    async with httpx.AsyncClient(timeout=20) as client:
        state["app"] = build(Settings.load(), client)
        yield
    state.clear()


api = FastAPI(title="Leiria traffic", lifespan=lifespan)
api.mount("/static", StaticFiles(directory=STATIC), name="static")

COOKIE = "lt_key"
LOOPBACK = {"127.0.0.1", "::1"}
LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Leiria traffic</title><body style="font-family:system-ui;max-width:22rem;margin:4rem auto;padding:0 1rem">
<h2>Leiria traffic</h2><form method="post" action="/login"><p>{msg}</p>
<input name="key" type="password" autofocus style="width:100%;padding:.5rem" placeholder="access key">
<p><button style="padding:.5rem 1rem">Open</button></p></form></body>"""


def _access_key() -> str:
    return getattr(getattr(state.get("app"), "settings", None), "access_key", "")


@api.middleware("http")
async def require_access_key(request: Request, call_next):
    """Remote clients need the LT_ACCESS_KEY cookie; this machine itself never does."""
    key = _access_key()
    host = request.client.host if request.client else ""
    if not key or host in LOOPBACK:
        return await call_next(request)
    if request.url.path == "/login" and request.method == "POST":
        given = parse_qs((await request.body()).decode()).get("key", [""])[0]
        if hmac.compare_digest(given, key):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, key, max_age=365 * 86400, httponly=True, samesite="strict")
            return resp
        return HTMLResponse(LOGIN_PAGE.format(msg="Wrong key."), status_code=401)
    if hmac.compare_digest(request.cookies.get(COOKIE, ""), key):
        return await call_next(request)
    if request.url.path == "/":
        return HTMLResponse(LOGIN_PAGE.format(msg="Access key (API_Keys.md, leiria-traffic):"), status_code=401)
    return JSONResponse({"detail": "access key required"}, status_code=401)


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


@api.get("/api/places")
def places() -> list[dict]:
    """Favourite places (Home, Work, ...)."""
    return _app().store.places()


@api.put("/api/places/{name}")
def save_place(name: str, point: str, label: str = "") -> dict:
    name = name.strip()
    if not name:
        raise HTTPException(422, "name is empty")
    _app().store.put_place(name, _point(point, "point"), label)
    return {"name": name}


@api.delete("/api/places/{name}")
def delete_place(name: str) -> dict:
    if not _app().store.delete_place(name):
        raise HTTPException(404, f"no favourite called {name!r}")
    return {"deleted": name}
