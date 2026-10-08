# leiria-traffic

How long does a car trip around Leiria **typically** take, for every 10-minute departure slot of a
given day, and when is the best time to leave near rush hour? Also includes a congestion heatmap of
Leiria.

The app **does not rebuild a traffic model**. It asks a routing provider for its own prediction for
that exact date and slot (TomTom first, Google as fallback). It adds only what the provider does not
expose (school calendar, holidays, weather and events as labelled context). Your own measured
percentiles come from a collector that samples live traffic every 10 minutes.

## Quick start

```properties
cd D:\Github\BD\leiria-traffic
uv sync
uv run leiria-traffic serve          # http://127.0.0.1:8765
```

- **Route profile tab:** pick A and B (search box or click the map), choose a date and a departure
  window, then press *Generate*. You get a chart and a table of 144 departures, the best and worst
  slot in the window, and the provider's no-traffic time. *Compare scenarios* overlays the next real
  date of each kind (term, school break, summer, public holiday) for the same weekday.
- **Favourites:** type a name (Home, Work, …) and press *★ save A* or *★ save B*. The chips then
  set A or B in one click, *⇅* swaps them, and *×* removes a favourite. Favourites live in the
  SQLite store, so they survive browser changes.
- **Leiria heatmap tab:** a 10×10 grid (±6 km around Praça Rodrigues Lobo) for one departure slot.
  - *Congestion* = predicted ÷ no-traffic time, both from TomTom.
  - *Minutes* = travel time to or from the centre.

### Shell

```properties
uv run leiria-traffic estimate 39.74362,-8.80705 39.6930,-8.8960 2026-10-13 08:14
uv run leiria-traffic profile  39.74362,-8.80705 39.6930,-8.8960 2026-10-13 --csv
uv run leiria-traffic add-route casa-maceira 39.74362,-8.80705 39.6930,-8.8960 --collect
uv run leiria-traffic collect        # what the scheduled task runs
```

## Configuration (environment variables or a repo-root `.env`, which is gitignored)

| Variable | Default | Meaning |
|---|---|---|
| `TOMTOM_API_KEY` | – | TomTom key (source: `BD_only\API_Keys.md` §5). Needed for routing, heatmap, geocoding and the collector |
| `GOOGLE_MAPS_API_KEY` | – | Google Routes API key for the fallback. Empty means the fallback is skipped |
| `LT_PROVIDERS` | `tomtom,google` | Order of the fallback chain |
| `LT_GOOGLE_BOUNDS` | `0` | `1` adds Google PESSIMISTIC and OPTIMISTIC calls (3× cost) to show a range |
| `LT_TOMTOM_QPS` | `5` | Client-side rate limit |
| `LT_CACHE_TTL_DAYS` | `7` | How long a provider answer is reused |
| `LT_RAIN_ADJUST_PCT` | `0` | Optional rain factor (off). If set, it is listed as `weather_adjustment` and never hidden inside the provider value |
| `LT_HOLIDAY_SUBDIV` | `10` | `holidays` subdivision (10 = Leiria district, which adds 22 May) |
| `LT_DB_PATH`, `LT_DATA_DIR` | `data/` | SQLite file and the folder holding the calendar and events YAML |

## API

| Endpoint | Returns |
|---|---|
| `GET /api/estimate?origin=lat,lon&dest=lat,lon&day=YYYY-MM-DD&at=HH:MM[&weather=auto\|dry\|rain]` | One departure, snapped down to the 10-minute grid |
| `GET /api/profile?origin=…&dest=…&day=…[&weather=…]` | 144 slots plus the best and worst slot, context and errors |
| `GET /api/scenarios?weekday=0..6` | The next real date for term, school break, summer and public holiday |
| `GET /api/heatmap?day=…&at=HH:MM&mode=time\|ratio&direction=in\|out` | Grid cells with minutes and ratio |
| `GET /api/geocode?q=…` | TomTom place search, biased to Leiria |
| `GET /api/places` · `PUT/DELETE /api/places/{name}?point=lat,lon` | Favourite places |
| `GET/POST /api/routes` | Saved routes. `collect=true` puts a route in the collector |

Example (live, 2026-10-08), Leiria centre → Maceira, Tuesday 13 Oct 2026:

```json
GET /api/estimate?origin=39.74362,-8.80705&dest=39.6930,-8.8960&day=2026-10-13&at=08:14
{"context": "Tuesday · school term · autumn · Mercado Levante",
 "slot": {"departure": "08:10", "duration_min": 17.7, "arrival": "08:27", "source": "provider_prediction",
          "provider": "tomtom", "typical_min": 17.7, "free_flow_min": 15.6, "distance_km": 11.53,
          "weather": "dry (open-meteo-forecast)", "adjustments": [], "measured": null}}
```

On that profile the night time is 15.5 min. The morning peak is 17.8 min at 08:20–08:40 and the
evening peak is worse, 19.9 min at 18:00.

## Where every number comes from

| `source` | Meaning |
|---|---|
| `provider_prediction` | The provider's own estimate for that date and slot. TomTom: `travelTimeInSeconds` with `departAt` in the future, which uses its historic speed profiles. `typical_min` = `historicTrafficTravelTimeInSeconds`, `free_flow_min` = `noTrafficTravelTimeInSeconds` |
| `measured_stats` | **Our** P50, P75 and P90 (plus mean, std, min, max, n) over collector samples for the same route, slot and weekday, excluding public holidays, with matching term/break and rain/dry. Shown only when n ≥ 3 |
| `weather_adjustment` | Our optional rain factor (`LT_RAIN_ADJUST_PCT`), off by default |
| `calendar_adjustment` | Reserved and not used. No provider documents a school-calendar effect, and we will not invent one. Compare scenarios or measured stats instead |

## Architecture

```
Browser (Leaflet + Chart.js) ─► FastAPI (api.py) ─► TravelTimeEngine (engine.py)
                                                  ├─ RoutingProvider: FallbackRouting[TomTom, Google]   routing.py
                                                  ├─ WeatherProvider: Open-Meteo forecast/archive        weather.py
                                                  ├─ SchoolCalendar (YAML) · Holidays (holidays lib) · Events (YAML)   calendar.py
                                                  └─ Store (SQLite): routes · predictions cache · observations · contexts   db.py
Task \BD\LeiriaTraffic\Collector (every 10 min, VMHOST1) ─► `leiria-traffic collect` ─► observations
```

- **Adding a provider:** implement `async route(origin, dest, departure) -> RouteEstimate`. The
  engine never imports a concrete provider.
- **Cache:** the key is (provider, origin, destination, slot departure). Points are rounded to 5
  decimals. Google answers are never stored (`cacheable=False`) because its terms forbid it.
- **Reliability:** retries use exponential backoff on 429, 5xx and network errors, and honour
  `Retry-After`. A per-provider rate limiter controls call rate. The API key is redacted from every
  error message. A failed slot shows its error and does not break the profile.
- **Data files:**
  - `data/school_calendar.yaml`: Despacho 8368/2024 + 9989/2025 + 10430/2026. Update it when a new
    despacho is published; dates it does not cover raise an error rather than guessing.
  - `data/events.yaml`: hand-maintained events.

## Research summary (2026-10-07, vendor documentation)

None of the four APIs exposes raw historical traffic. Each gives its own prediction built from its
historical patterns. Full notes and URLs are in [docs/research.md](docs/research.md).

| | TomTom (primary) | Google (fallback) | HERE | Mapbox |
|---|---|---|---|---|
| Future departure | ✔ | ✔ | ✔ (past too) | ✔ |
| Typical / no-traffic / live in one call | **✔** | 2 values | ✔ | 2 values |
| Range | ✘ | ✔ pessimistic/optimistic | ✘ | ✘ |
| Free / month | 20 000 routes | 5 000 | ? | 100 000 |
| Paid | €1 / 1 000 | $10 / 1 000 | ? | $2 / 1 000 |
| Storing results | per cache headers | forbidden | ≤ 30 days | ? |

## Limitations

- **TomTom's prediction is smooth.** It is a typical profile, so on a 12 km trip the rush-hour cost
  shows as a few minutes. It does not capture day-to-day variance. That variance is what
  `measured_stats` is for, and it needs weeks of collector data per route.
- **School term vs holidays.** No provider documents using the school calendar. *Compare scenarios*
  shows whatever difference the provider's own profiles contain, nothing more.
- **Weather.** No provider documents weather as a prediction input. Forecasts reach 16 days ahead;
  further out, weather is shown as unknown. A rain factor is available but off. The US Federal
  Highway Administration gives +11 to +50 % arterial delay, too wide to apply blindly
  (https://ops.fhwa.dot.gov/weather/q1_roadimpact.htm).
- **Past dates.** TomTom rejects past departures for the matrix and does not document them for
  single routes. For history use the collector. HERE would allow it if added as a provider.
- **Costs.** A profile is 144 calls, a scenario comparison 576, a heatmap slot 100. Everything is
  cached for 7 days. The free tier covers about 130 new profiles a month.
- **Terms.** The TomTom terms limit storing results to what the cache headers allow, for a single
  user. This is a personal tool and the cache is local.
- **Google fallback.** Coded but off: the Routes API needs a billing account even inside the free
  calls, and this project spends nothing. No free provider without a card offers future-departure
  traffic (see `docs/research.md`), so TomTom is the only active provider.
- **Logs.** httpx is kept at WARNING because its INFO line prints the request URL, which carries the
  TomTom key.

## Tests

```properties
uv run pytest -q
```

They cover the slot grid, percentiles, the school calendar (amended dates and the uncovered-date
error), Leiria holidays, the engine (144 slots, cache, non-cacheable Google results, fallback, rain
labelling, measured stats, scenarios) and provider parsing with mocked HTTP (TomTom, 429 retry, key
redaction, Google bounds, Open-Meteo, heatmap cache).
