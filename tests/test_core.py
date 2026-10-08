from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from leiria_traffic.calendar import season
from leiria_traffic.engine import TZ
from leiria_traffic.models import SLOTS_PER_DAY, Point, RouteEstimate, slot_times, snap_to_slot
from leiria_traffic.routing import FallbackRouting, ProviderError
from leiria_traffic.stats import percentile, summarize

from .conftest import A, B, MONDAY_TERM, FakeRouting, FakeWeather


# ---------------------------------------------------------------- grid & stats
def test_slot_grid_is_144_ten_minute_slots():
    s = slot_times(date(2026, 10, 12))
    assert len(s) == SLOTS_PER_DAY == 144
    assert (s[0], s[1], s[-1]) == (time(0, 0), time(0, 10), time(23, 50))


@pytest.mark.parametrize("t,expected", [(time(8, 0), time(8, 0)), (time(8, 9), time(8, 0)), (time(8, 10), time(8, 10)),
                                        (time(23, 59), time(23, 50))])
def test_snap_to_slot_rounds_down(t, expected):
    assert snap_to_slot(t) == expected


def test_percentiles_match_numpy_linear():
    v = sorted([28, 30, 31, 33, 34, 36, 41, 45])
    assert percentile(v, 50) == pytest.approx(33.5)
    assert percentile(v, 75) == pytest.approx(37.25)
    assert percentile(v, 90) == pytest.approx(42.2)
    assert percentile([5.0], 90) == 5.0


def test_summarize():
    assert summarize([]) is None
    s = summarize([10, 20, 30])
    assert (s["n"], s["p50"], s["min"], s["max"], s["mean"]) == (3, 20, 10, 30, 20)
    assert s["std"] == pytest.approx(10)


def test_point_parse_and_key():
    p = Point.parse("39.7436,-8.8071")
    assert p.key() == "39.74360,-8.80710"
    with pytest.raises(ValueError):
        Point.parse("95,0")


def test_route_estimate_arrival_and_delay():
    dep = datetime(2026, 10, 12, 8, 10, tzinfo=TZ)
    e = RouteEstimate("x", dep, 1064, free_flow_s=933)
    assert e.arrival == dep + timedelta(seconds=1064)
    assert e.delay_s == 131


# ---------------------------------------------------------------- calendar
@pytest.mark.parametrize("day,term,brk", [
    (date(2026, 10, 12), True, None),          # Monday in term 1
    (date(2025, 12, 22), False, "natal"),      # amended Christmas break (Despacho 9989/2025)
    (date(2025, 12, 16), True, None),          # last class day before the amended break
    (date(2026, 2, 17), False, "carnaval"),
    (date(2026, 4, 6), False, "pascoa"),
    (date(2026, 7, 15), False, "summer"),
    (date(2026, 10, 10), False, "weekend"),
])
def test_school_calendar(contexts, day, term, brk):
    assert contexts.school.status(day) == (term, brk)


def test_school_calendar_refuses_uncovered_dates(contexts):
    with pytest.raises(LookupError):
        contexts.school.status(date(2030, 1, 7))


@pytest.mark.parametrize("day,name_part", [(date(2026, 5, 22), "Leiria"), (date(2026, 6, 4), ""),
                                            (date(2026, 12, 25), ""), (date(2026, 10, 5), "")])
def test_holidays_include_national_and_leiria(contexts, day, name_part):
    name = contexts.hols.holiday(day)
    assert name and name_part in name


def test_holiday_overrides_school_term(contexts):
    c = contexts.build(date(2026, 12, 8))  # Imaculada Conceição, a Tuesday in term 1
    assert c.public_holiday and not c.school_term


def test_recurring_event_and_label(contexts):
    c = contexts.build(date(2026, 10, 13))  # Tuesday
    assert "Mercado Levante" in c.events
    assert "school term" in c.label() and "Tuesday" in c.label()


def test_season():
    assert season(date(2026, 8, 1)) == "summer holidays"
    assert season(date(2026, 1, 15)) == "winter"


# ---------------------------------------------------------------- engine
async def test_profile_has_144_slots_and_finds_peak(make_engine):
    p = await make_engine().profile(A, B, MONDAY_TERM)
    assert len(p["slots"]) == 144
    s0810 = next(s for s in p["slots"] if s["departure"] == "08:10")
    assert (s0810["duration_min"], s0810["arrival"], s0810["source"]) == (25.0, "08:35", "provider_prediction")
    assert p["summary"]["worst"]["minutes"] == 25.0 and p["summary"]["best"]["minutes"] == 15.0
    assert "school term" in p["context"]


async def test_second_profile_is_served_from_cache(make_engine):
    routing = FakeRouting()
    eng = make_engine(routing)
    await eng.profile(A, B, MONDAY_TERM)
    await eng.profile(A, B, MONDAY_TERM)
    assert routing.calls == 144


async def test_non_cacheable_results_are_not_stored(make_engine):
    routing = FakeRouting("google", cacheable=False)
    eng = make_engine(routing)
    await eng.estimate(A, B, MONDAY_TERM, time(8, 10))
    await eng.estimate(A, B, MONDAY_TERM, time(8, 10))
    assert routing.calls == 2


async def test_estimate_snaps_to_slot(make_engine):
    r = await make_engine().estimate(A, B, MONDAY_TERM, time(8, 14))
    assert r["slot"]["departure"] == "08:10"


async def test_rain_adjustment_is_off_by_default_and_labelled_when_on(make_engine):
    rain = FakeWeather(rain_hours=[8])
    off = await make_engine(weather=rain).estimate(A, B, MONDAY_TERM, time(8, 0))
    assert off["slot"]["duration_min"] == 25.0 and off["slot"]["adjustments"] == []
    on = await make_engine(weather=rain, rain_adjust_pct=10).estimate(A, B, MONDAY_TERM, time(8, 30))
    adj = on["slot"]["adjustments"]
    assert on["slot"]["duration_min"] == 27.5
    assert adj[0]["source"] == "weather_adjustment" and adj[0]["seconds"] == 150


async def test_weather_override(make_engine):
    r = await make_engine(rain_adjust_pct=10).estimate(A, B, MONDAY_TERM, time(12, 0), weather="rain")
    assert r["slot"]["adjustments"] and "override" in r["slot"]["weather"]


async def test_fallback_uses_second_provider_and_reports_total_failure(make_engine):
    eng = make_engine(FallbackRouting([FakeRouting("tomtom", fail=True), FakeRouting("google")]))
    r = await eng.estimate(A, B, MONDAY_TERM, time(9, 0))
    assert r["slot"]["provider"] == "google"
    dead = make_engine(FallbackRouting([FakeRouting("tomtom", fail=True)]))
    r = await dead.estimate(A, B, MONDAY_TERM, time(9, 0))
    assert r["slot"]["duration_min"] is None and "tomtom down" in r["slot"]["error"]
    none = make_engine(FallbackRouting([]))
    r = await none.estimate(A, B, MONDAY_TERM, time(9, 0))
    assert "no routing provider" in r["slot"]["error"]


async def test_measured_percentiles_need_min_samples(make_engine, store):
    eng = make_engine()
    rid = store.add_route("home-work", A, B, collect=True)
    # 4 term Mondays without holidays (5 Oct, Implantação da República, would be excluded)
    mondays = [MONDAY_TERM + timedelta(days=7 * k) for k in range(1, 5)]
    for k, day in enumerate(mondays):
        eng.context(day)
        e = RouteEstimate("tomtom", datetime.combine(day, time(8, 10), TZ), 1500 + 60 * k, live_s=1500 + 60 * k)
        store.add_observation(rid, datetime.combine(day, time(8, 12), TZ), "08:10", e, 0.0, 15.0)
    r = await eng.estimate(A, B, MONDAY_TERM, time(8, 10))
    m = r["slot"]["measured"]
    assert m["source"] == "measured_stats" and m["n"] == 4
    assert (m["p50"], m["p90"]) == (26.5, 27.7)
    r = await eng.estimate(A, B, MONDAY_TERM, time(8, 10), weather="rain")  # no rainy samples
    assert r["slot"]["measured"] is None


def test_scenario_dates(make_engine):
    found = {s["scenario"]: s for s in make_engine().scenario_dates(0, date(2026, 10, 8))}
    assert found["school term"]["date"] == "2026-10-12"
    assert found["school break"]["date"] == "2026-12-21"
    assert found["summer holidays"]["date"] == "2027-07-05"
    for s in found.values():
        assert date.fromisoformat(s["date"]).weekday() == 0


def test_favourite_places_api(store):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from leiria_traffic import api

    api.state["app"] = SimpleNamespace(store=store)
    try:
        c = TestClient(api.api)  # no `with`: the lifespan (real providers) does not run
        assert c.put("/api/places/Home", params={"point": "39.75000,-8.81000", "label": "home"}).status_code == 200
        c.put("/api/places/Work", params={"point": "39.69000,-8.89000"})
        c.put("/api/places/Home", params={"point": "39.74,-8.80"})  # re-saving moves it
        assert c.get("/api/places").json() == [{"name": "Home", "point": "39.74000,-8.80000", "label": ""},
                                               {"name": "Work", "point": "39.69000,-8.89000", "label": ""}]
        assert c.put("/api/places/Bad", params={"point": "nowhere"}).status_code == 422
        assert c.delete("/api/places/Work").status_code == 200
        assert c.delete("/api/places/Work").status_code == 404
        assert [p["name"] for p in c.get("/api/places").json()] == ["Home"]
    finally:
        api.state.clear()


def test_remote_clients_need_the_access_key(store):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from leiria_traffic import api

    api.state["app"] = SimpleNamespace(store=store, settings=SimpleNamespace(access_key="k3y"))
    try:
        c = TestClient(api.api)  # client host is "testclient", so it counts as remote
        assert c.get("/api/places").status_code == 401
        page = c.get("/")
        assert page.status_code == 200 and 'action="/login"' in page.text
        assert c.post("/login", data={"key": "wrong"}).status_code == 401
        ok = c.post("/login", data={"key": "k3y"}, follow_redirects=False)
        assert ok.status_code == 303 and "httponly" in ok.headers["set-cookie"].lower()
        assert c.get("/api/places").status_code == 200  # the cookie now rides along
    finally:
        api.state.clear()


def test_lan_serve_refuses_without_access_key(monkeypatch):
    from leiria_traffic import cli

    monkeypatch.setenv("LT_ACCESS_KEY", "")
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
