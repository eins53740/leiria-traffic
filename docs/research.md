# Phase 1 research: traffic APIs and context sources (2026-10-07)

All claims below come from vendor or primary pages fetched on 2026-10-07. "Not documented" means the
page is silent on the point; it does not mean the answer is no.

Categories used in the tables: **A** = raw historical data · **B** = the provider's own prediction ·
**C** = real-time traffic · **D** = routing without traffic.

## Routing APIs

| | Google Routes | HERE Routing v8 | TomTom Routing | Mapbox Directions |
|---|---|---|---|---|
| Real-time (C) | ✔ | ✔ | ✔ | ✔ |
| Raw history (A) | ✘ | ✘ (separate Traffic Analytics product) | ✘ (Traffic Stats / O/D Analysis, sales only) | ✘ (Traffic Data licence) |
| Future departure (B) | ✔, no maximum horizon documented for DRIVE | ✔, and past departures (history only) | ✔. Matrix must be in the future | ✔. Matrix `depart_at` is beta |
| Historical patterns used internally | ✔ | ✔ | ✔ | ✔ |
| Separate durations | `duration`, `staticDuration` (definitions contradict each other) | `duration` / `typicalDuration` / `baseDuration` | `travelTimeInSeconds` + `noTraffic…` / `historicTraffic…` / `liveTrafficIncidents…TravelTimeInSeconds` + `trafficDelayInSeconds` | `duration`, `duration_typical` |
| Uncertainty | `trafficModel` PESSIMISTIC / OPTIMISTIC (only with TRAFFIC_AWARE_OPTIMAL) | not documented | not documented | not documented |
| Limits | 3 000 QPM; matrix 625 elements (100 with OPTIMAL) | matrix 500×500 sync (Region mode) | queries per second capped, value not documented; matrix 100 cells sync | 300 req/min; matrix 10 coordinates, 30 req/min |
| Free / month | 5 000 (Pro SKU) | not extracted (pricing page returned 403) | 20 000 routing; 2 500 matrix | 100 000 |
| Paid, first tier | $10 / 1 000 | not extracted | €1 / 1 000 routing; €3 / 1 000 matrix | $2 / 1 000 |
| Portugal traffic coverage | ✔ | ✔ | ✔ | not confirmed |
| Storing results | forbidden (only lat/lng for 30 days) | ≤ 30 days, within cache headers | within cache headers, not for serving multiple users | not verified |

Sources:

- **Google**
  - computeRoutes reference: https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes
  - TrafficModel: https://developers.google.com/maps/documentation/routes/reference/rest/v2/TrafficModel
  - Trade-offs (`staticDuration` described as "historical only"): https://developers.google.com/maps/documentation/routes/config_trade_offs
  - Billing: https://developers.google.com/maps/documentation/routes/usage-and-billing
  - Pricing: https://mapsplatform.google.com/pricing/ (in March 2025 the $200 monthly credit was replaced by free calls per SKU)
  - Terms: https://cloud.google.com/maps-platform/terms/maps-service-terms
- **HERE**
  - Durations: https://docs.here.com/routing/docs/routing-v8-duration.md
  - Time-dependent routing: https://docs.here.com/routing/docs/routing-v8-time-dependent-routing.md
  - Matrix traffic: https://docs.here.com/routing/docs/matrix-v8-traffic.md
  - Traffic coverage: https://docs.here.com/traffic-api/docs/traffic-coverage-information.md
- **TomTom**
  - calculateRoute: https://developer.tomtom.com/routing-api/documentation/tomtom-maps/calculate-route
  - Synchronous matrix: https://docs.tomtom.com/routing-api/documentation/tomtom-maps/matrix-routing-v2/synchronous-matrix
  - Pricing: https://docs.tomtom.com/pricing/
  - Market coverage: https://docs.tomtom.com/routing-api/documentation/tomtom-maps/product-information/market-coverage
  - Terms (§11.4 caching, §14.1 rate limits): https://developer.tomtom.com/terms-and-conditions
- **Mapbox**
  - Directions: https://docs.mapbox.com/api/navigation/directions/
  - Matrix: https://docs.mapbox.com/api/navigation/matrix/

**Decision.** TomTom is the primary provider: it is the only API that returns typical, no-traffic and
live times in one call, and it is the cheapest paid option. Google is the fallback and the only
source of a provider range (chosen by Bruno, 2026-10-08). HERE is the candidate for past-date
scenarios if that becomes necessary.

## Context sources

| Factor | Source | Notes |
|---|---|---|
| School calendar | Despacho 8368/2024 (https://diariodarepublica.pt/dr/detalhe/despacho/8368-2024-873447631) + 9989/2025 + 10430/2026 | No API or ICS feed, so the dates live in `data/school_calendar.yaml` |
| Holidays | Python `holidays`, PT subdivision 10 (https://github.com/vacanza/holidays) | Includes 22 May "Dia do Município de Leiria". Nager.Date has no municipal holidays |
| Weather | Open-Meteo forecast (16 days) and archive (https://open-meteo.com/en/docs) | Free for non-commercial use, under 10 000 calls/day, CC-BY 4.0. IPMA (`globalIdLocal` 1100900) forecasts only 5 days ahead, daily values |
| Rain effect | FHWA Road Weather Management (https://ops.fhwa.dot.gov/weather/q1_roadimpact.htm) | Arterials: −10 to −25 % speed, +11 to +50 % delay. Used only as an optional, labelled factor |
| Provider weather input | Google: not documented. TomTom: weather feeds live traffic, prediction up to 24 h. HERE: not documented | Hence no double counting: providers do not claim to model weather in future predictions |
| Events | No feed. `data/events.yaml` | The 2026 Feira de Maio was cancelled; the Levante market runs Tuesdays and Saturdays at the stadium car park |
