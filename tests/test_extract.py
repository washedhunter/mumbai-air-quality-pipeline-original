"""Parsing, the OpenAQ clients (mocked HTTP), extraction to landing and validation."""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import date

import httpx
import pytest

from mumbai_aq import parse
from mumbai_aq.extract import extract_day, read_manifest
from mumbai_aq.openaq import OpenAQApi, OpenAQArchive, OpenAQError
from mumbai_aq.validate import ValidationError, validate_day

DAY = date(2026, 11, 8)


def archive_csv(location_id=6945, rows=None) -> bytes:
    rows = rows or [
        ("2026-11-08T00:45:00+05:30", "pm25", "µg/m³", "151.4"),
        ("2026-11-08T01:00:00+05:30", "pm25", "µg/m³", "104.0"),
        ("2026-11-08T01:00:00+05:30", "co", "ppb", "0.52"),
    ]
    lines = ['"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"']
    for ts, p, u, v in rows:
        lines.append(f'{location_id},12235834,"Kurla, Mumbai - MPCB","{ts}","19.0863","72.8888","{p}","{u}","{v}"')
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))


def measurements_json(values=(270.58, 220.07)) -> bytes:
    results = []
    for i, v in enumerate(values):
        results.append(
            {
                "value": v,
                "parameter": {"id": 1, "name": "pm10", "units": "Âµg/mÂ³"},  # as the API sends it
                "period": {
                    "datetimeFrom": {"utc": f"2026-11-07T19:{i * 15:02d}:00Z"},
                    "datetimeTo": {"utc": f"2026-11-07T19:{i * 15 + 15:02d}:00Z"},
                },
            }
        )
    return json.dumps({"meta": {"found": len(results)}, "results": results}).encode()


def locations_json() -> bytes:
    def loc(i, first, last, sensors):
        return {
            "id": i,
            "name": f"Station {i}",
            "provider": {"name": "CPCB"},
            "coordinates": {"latitude": 19.0, "longitude": 72.8},
            "datetimeFirst": {"utc": first},
            "datetimeLast": {"utc": last},
            "sensors": [{"id": s, "parameter": {"name": p, "units": "Âµg/mÂ³"}} for s, p in sensors],
        }

    return json.dumps(
        {
            "results": [
                loc(6945, "2019-06-19T00:00:00Z", "2026-11-14T06:00:00Z", [(11, "pm10"), (12, "temperature")]),
                loc(7850, "2020-01-09T00:00:00Z", "2021-12-24T00:00:00Z", [(21, "pm25")]),  # inactive
            ]
        }
    ).encode()


# ------------------------------------------------------------------ parse
def test_parse_archive_csv():
    df = parse.parse_archive_csv(archive_csv(), 6945)
    assert len(df) == 3
    assert df["day_ist"].astype(str).unique().tolist() == ["2026-11-08"]
    assert df["units"].iloc[0] == "µg/m³"


def test_parse_archive_rejects_wrong_location_and_bad_gzip():
    with pytest.raises(parse.PayloadError):
        parse.parse_archive_csv(archive_csv(location_id=1), 6945)
    with pytest.raises(parse.PayloadError):
        parse.parse_archive_csv(b"not gzip", 6945)


def test_parse_measurements_uses_period_end_and_fixes_units():
    df = parse.parse_measurements_json(measurements_json(), 6945, 11)
    assert df["units"].tolist() == ["µg/m³", "µg/m³"]
    assert str(df["ts_utc"].iloc[0]) == "2026-11-07 19:15:00+00:00"
    assert df["day_ist"].astype(str).tolist() == ["2026-11-08", "2026-11-08"]


def test_parse_locations():
    locs = parse.parse_locations_json(locations_json())
    assert [loc["location_id"] for loc in locs] == [6945, 7850]
    assert locs[0]["sensors"][0] == {"id": 11, "parameter": "pm10", "units": "µg/m³"}


def test_fix_units_leaves_clean_text_alone():
    assert parse.fix_units("ppb") == "ppb"
    assert parse.fix_units("Âµg/mÂ³") == "µg/m³"


# ------------------------------------------------------------------ clients
def _settings(**kw):
    from mumbai_aq.config import Settings

    return replace(Settings(), openaq_api_key="test-key", http_max_retries=3, **kw)


def test_api_retries_rate_limit_using_reset_header():
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset": "7"})
        return httpx.Response(200, content=measurements_json(), headers={"x-ratelimit-remaining": "50"})

    s = _settings()
    client = httpx.Client(base_url=s.openaq_base_url, transport=httpx.MockTransport(handler))
    api = OpenAQApi(s, client=client, sleep=sleeps.append)
    got = api.measurements(11, DAY)
    assert got.status == 200 and sleeps == [7.0]
    params = dict(calls[-1].url.params)
    # IST day 2026-11-08 = 2026-11-07T18:30Z .. 2026-11-08T18:30Z, start exclusive
    assert params["datetime_from"].startswith("2026-11-07T18:30:01")
    assert params["datetime_to"].startswith("2026-11-08T18:30:00")


def test_api_pauses_when_window_nearly_used():
    sleeps = []
    handler = lambda r: httpx.Response(  # noqa: E731
        200, content=locations_json(), headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "12"}
    )
    s = _settings()
    api = OpenAQApi(s, client=httpx.Client(base_url=s.openaq_base_url, transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    api.locations()
    assert sleeps == [12.0]


def test_api_gives_up_after_retries_and_on_client_errors():
    s = _settings()
    api = OpenAQApi(
        s,
        client=httpx.Client(base_url=s.openaq_base_url, transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        sleep=lambda _: None,
    )
    with pytest.raises(OpenAQError, match="503"):
        api.locations()
    api401 = OpenAQApi(
        s,
        client=httpx.Client(base_url=s.openaq_base_url, transport=httpx.MockTransport(lambda r: httpx.Response(401))),
        sleep=lambda _: None,
    )
    with pytest.raises(OpenAQError, match="401"):
        api401.locations()


def test_api_requires_key():
    from mumbai_aq.config import Settings

    with pytest.raises(OpenAQError, match="OPENAQ_API_KEY"):
        OpenAQApi(replace(Settings(), openaq_api_key=""))


def test_archive_url_and_missing_day():
    s = _settings()
    arch = OpenAQArchive(s, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    assert arch.url(6945, DAY).endswith(
        "/records/csv.gz/locationid=6945/year=2026/month=11/location-6945-20261108.csv.gz"
    )
    got = arch.day(6945, DAY)
    assert got.status == 404 and got.body is None


# ------------------------------------------------------------------ extract + validate
def _api(s, extra_handler=None):
    def handler(request):
        path = request.url.path
        if path.endswith("/locations"):
            return httpx.Response(200, content=locations_json())
        if "/sensors/11/measurements" in path:
            return httpx.Response(200, content=measurements_json())
        raise AssertionError(f"unexpected request {request.url}")

    return OpenAQApi(s, client=httpx.Client(base_url=s.openaq_base_url, transport=httpx.MockTransport(handler)), sleep=lambda _: None)


def test_extract_api_only_fetches_active_aqi_sensors(tmp_path):
    s = _settings(extract_source="api")
    res = extract_day(DAY, s, landing_root=tmp_path, api=_api(s))
    entries = read_manifest(tmp_path, DAY)
    assert res["source"] == "api"
    assert [(e.endpoint, e.sensor_id) for e in entries] == [("locations", None), ("measurements", 11)]
    summary = validate_day(tmp_path, DAY)
    assert summary["readings"] == 2 and summary["locations_with_data"] == 1


def test_extract_archive_without_key_uses_seed_list(tmp_path):
    from mumbai_aq.config import Settings

    s = replace(Settings(), openaq_api_key="", extract_source="archive")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if "locationid=6945/" in request.url.path:
            return httpx.Response(200, content=archive_csv())
        return httpx.Response(404)

    arch = OpenAQArchive(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    extract_day(DAY, s, landing_root=tmp_path, archive=arch)
    entries = read_manifest(tmp_path, DAY)
    assert len(entries) == len(seen) == 44  # every location in config/stations.csv
    assert sum(e.http_status == 200 for e in entries) == 1
    assert validate_day(tmp_path, DAY)["readings"] == 3


def test_auto_source_switches_to_archive_for_old_days():
    s = _settings(extract_source="auto", archive_min_age_days=6)
    assert s.source_for(date(2026, 11, 10), today=date(2026, 11, 14)) == "api"
    assert s.source_for(date(2026, 11, 8), today=date(2026, 11, 14)) == "archive"


def test_extract_rerun_replaces_landing_folder(tmp_path):
    s = _settings(extract_source="api")
    stale = tmp_path / DAY.isoformat() / "stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    extract_day(DAY, s, landing_root=tmp_path, api=_api(s))
    assert not stale.exists()


def test_validate_catches_tampering_and_empty_days(tmp_path):
    s = _settings(extract_source="api")
    extract_day(DAY, s, landing_root=tmp_path, api=_api(s))
    f = tmp_path / DAY.isoformat() / "measurements-6945-11.json"
    f.write_bytes(f.read_bytes() + b" ")
    with pytest.raises(ValidationError, match="sha256"):
        validate_day(tmp_path, DAY)

    def empty_handler(request):
        if request.url.path.endswith("/locations"):
            return httpx.Response(200, content=locations_json())
        return httpx.Response(200, content=json.dumps({"results": []}).encode())

    api = OpenAQApi(s, client=httpx.Client(base_url=s.openaq_base_url, transport=httpx.MockTransport(empty_handler)), sleep=lambda _: None)
    extract_day(DAY, s, landing_root=tmp_path, api=api)
    with pytest.raises(ValidationError, match="no readings"):
        validate_day(tmp_path, DAY)


def test_validate_rejects_readings_from_another_day(tmp_path):
    from mumbai_aq.config import Settings

    s = replace(Settings(), openaq_api_key="", extract_source="archive")
    wrong = archive_csv(rows=[("2026-10-01T10:00:00+05:30", "pm25", "µg/m³", "50")])
    arch = OpenAQArchive(
        s,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=wrong) if "6945" in r.url.path else httpx.Response(404))),
    )
    extract_day(DAY, s, landing_root=tmp_path, archive=arch, location_ids={6945})
    with pytest.raises(ValidationError, match="fall on or next to"):
        validate_day(tmp_path, DAY)


def test_sample_source_replays_committed_payloads(settings_env, tmp_path):
    s = replace(settings_env(), extract_source="sample")
    res = extract_day(date(2026, 11, 8), s, landing_root=tmp_path)
    assert res["payloads"] == 6
    assert validate_day(tmp_path, date(2026, 11, 8))["locations_with_data"] == 6
    with pytest.raises(FileNotFoundError):
        extract_day(date(2020, 1, 1), s, landing_root=tmp_path)
