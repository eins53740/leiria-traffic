"""Command line: run the web app, sample live traffic (collector), query from the shell.

  leiria-traffic serve [--port 8765]
  leiria-traffic collect                      # one live sample per collecting route
  leiria-traffic add-route NAME A B [--collect]
  leiria-traffic estimate A B YYYY-MM-DD HH:MM
  leiria-traffic profile  A B YYYY-MM-DD [--csv]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
from datetime import date, datetime, time

import httpx

from .config import Settings
from .engine import TZ
from .models import Point, snap_to_slot
from .routing import ProviderError
from .wiring import build

log = logging.getLogger("leiria_traffic")


async def collect(settings: Settings) -> int:
    """One live sample for every route with collect=1. Exit 1 if any sample failed."""
    async with httpx.AsyncClient(timeout=20) as client:
        app = build(settings, client)
        if app.tomtom is None:
            log.error("collector needs TOMTOM_API_KEY")
            return 2
        now = datetime.now(TZ)
        slot = snap_to_slot(now.time()).strftime("%H:%M")
        app.engine.context(now.date())
        failures = 0
        for r in app.store.routes(collecting_only=True):
            o, d = Point.parse(r["origin"]), Point.parse(r["destination"])
            try:
                e = await app.tomtom.route(o, d, None)  # departAt=now -> live traffic
            except ProviderError as err:
                log.error("route %s: %s", r["name"], err)
                failures += 1
                continue
            w = (await app.engine.weather.day(o, now.date())).get(now.hour)
            app.store.add_observation(r["route_id"], now, slot, e,
                                      None if w is None else w.precipitation_mm,
                                      None if w is None else w.temperature_c)
            log.info("route %s %s: %.1f min (free flow %.1f)", r["name"], slot, (e.live_s or e.duration_s) / 60,
                     (e.free_flow_s or 0) / 60)
        return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="leiria-traffic")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765)
    sub.add_parser("collect")
    a = sub.add_parser("add-route"); a.add_argument("name"); a.add_argument("origin"); a.add_argument("dest")
    a.add_argument("--collect", action="store_true")
    e = sub.add_parser("estimate"); e.add_argument("origin"); e.add_argument("dest"); e.add_argument("day"); e.add_argument("at")
    f = sub.add_parser("profile"); f.add_argument("origin"); f.add_argument("dest"); f.add_argument("day")
    f.add_argument("--csv", action="store_true")
    args = p.parse_args(argv)
    settings = Settings.load()

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("leiria_traffic.api:api", host=args.host, port=args.port)
        return 0
    if args.cmd == "collect":
        # runs hidden from Task Scheduler: keep a small rotating log next to the DB
        fh = logging.handlers.RotatingFileHandler(settings.data_dir / "collector.log", maxBytes=1_000_000,
                                                  backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        return asyncio.run(collect(settings))
    if args.cmd == "add-route":
        from .db import Store
        rid = Store(settings.db_path).add_route(args.name, Point.parse(args.origin), Point.parse(args.dest),
                                                collect=args.collect)
        print(f"route {rid} saved")
        return 0

    async def run():
        async with httpx.AsyncClient(timeout=20) as client:
            eng = build(settings, client).engine
            o, d = Point.parse(args.origin), Point.parse(args.dest)
            if args.cmd == "estimate":
                return await eng.estimate(o, d, date.fromisoformat(args.day), time.fromisoformat(args.at))
            return await eng.profile(o, d, date.fromisoformat(args.day))

    out = asyncio.run(run())
    if args.cmd == "profile" and args.csv:
        print("departure,duration_min,arrival,typical_min,free_flow_min,provider,weather")
        for sl in out["slots"]:
            print(",".join(str(sl[k] if sl[k] is not None else "") for k in
                           ("departure", "duration_min", "arrival", "typical_min", "free_flow_min", "provider", "weather")))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
