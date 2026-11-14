"""Extract one IST day of readings into a landing folder, plus a manifest describing every request.

landing/<day>/manifest.json lists each payload (URL, status, sha256, file); the bytes sit next to it
exactly as received. validate.py checks the folder, load.py copies it into raw.payloads.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import IST, Settings
from .openaq import Fetched, OpenAQApi, OpenAQArchive
from .parse import parse_locations_json

AQI_PARAMETERS = {"pm25", "pm10", "no2", "so2", "o3", "co", "nh3"}


@dataclass
class ManifestEntry:
    source: str
    endpoint: str
    location_id: int | None
    sensor_id: int | None
    target_date: str
    url: str
    http_status: int
    content_type: str | None
    file: str | None
    sha256: str | None
    fetched_at: str


def day_dir(landing_root: Path, day: date) -> Path:
    return landing_root / day.isoformat()


def read_manifest(landing_root: Path, day: date) -> list[ManifestEntry]:
    path = day_dir(landing_root, day) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"no manifest for {day} in {landing_root}")
    return [ManifestEntry(**e) for e in json.loads(path.read_text(encoding="utf-8"))["entries"]]


def _write(
    folder: Path,
    entries: list[ManifestEntry],
    fetched: Fetched,
    *,
    source: str,
    endpoint: str,
    day: date,
    location_id: int | None,
    sensor_id: int | None,
    filename: str,
) -> None:
    file = None
    digest = None
    if fetched.body is not None:
        (folder / filename).write_bytes(fetched.body)
        file = filename
        digest = hashlib.sha256(fetched.body).hexdigest()
    entries.append(
        ManifestEntry(
            source=source,
            endpoint=endpoint,
            location_id=location_id,
            sensor_id=sensor_id,
            target_date=day.isoformat(),
            url=fetched.url,
            http_status=fetched.status,
            content_type=fetched.content_type,
            file=file,
            sha256=digest,
            fetched_at=fetched.fetched_at.isoformat(),
        )
    )


def _active_on(loc: dict, day: date) -> bool:
    """Location reported within a day of `day` (dates in IST); unknown ranges count as active."""
    first, last = loc.get("datetime_first"), loc.get("datetime_last")
    if first is not None and first.astimezone(IST).date() > day + timedelta(days=1):
        return False
    return not (last is not None and last.astimezone(IST).date() < day - timedelta(days=1))


def _seed_location_ids(settings: Settings) -> list[int]:
    with settings.stations_file.open(encoding="utf-8") as fh:
        return sorted({int(r["location_id"]) for r in csv.DictReader(fh)})


def extract_day(
    day: date,
    settings: Settings,
    landing_root: Path | None = None,
    today: date | None = None,
    api: OpenAQApi | None = None,
    archive: OpenAQArchive | None = None,
    location_ids: set[int] | None = None,
) -> dict:
    """Fetch everything for `day`; returns a small summary (safe to pass through XCom)."""
    landing_root = landing_root or settings.landing_dir
    today = today or datetime.now(IST).date()
    source = settings.source_for(day, today)
    folder = day_dir(landing_root, day)
    if folder.exists():
        shutil.rmtree(folder)  # a re-run starts from a clean folder

    if source == "sample":
        src = day_dir(settings.sample_dir / "landing", day)
        if not (src / "manifest.json").exists():
            raise FileNotFoundError(f"no committed sample for {day} in {settings.sample_dir}")
        shutil.copytree(src, folder)
        entries = read_manifest(landing_root, day)
        return {"day": day.isoformat(), "source": source, "payloads": len(entries)}

    folder.mkdir(parents=True)
    entries: list[ManifestEntry] = []

    # 1. Discover stations (raw response kept). Without a key, archive mode uses the seed list.
    locations: list[dict]
    if source == "api" or settings.openaq_api_key:
        api = api or OpenAQApi(settings)
        fetched = api.locations()
        _write(
            folder,
            entries,
            fetched,
            source="api",
            endpoint="locations",
            day=day,
            location_id=None,
            sensor_id=None,
            filename="locations.json",
        )
        assert fetched.body is not None
        locations = [loc for loc in parse_locations_json(fetched.body) if _active_on(loc, day)]
    else:
        locations = [{"location_id": i, "sensors": []} for i in _seed_location_ids(settings)]

    if location_ids is not None:
        locations = [loc for loc in locations if loc["location_id"] in location_ids]

    # 2. Readings for the day.
    if source == "archive":
        archive = archive or OpenAQArchive(settings)
        for loc in locations:
            lid = loc["location_id"]
            fetched = archive.day(lid, day)
            _write(
                folder,
                entries,
                fetched,
                source="archive",
                endpoint="archive_day",
                day=day,
                location_id=lid,
                sensor_id=None,
                filename=f"archive-{lid}.csv.gz",
            )
    elif source == "api":
        assert api is not None
        for loc in locations:
            for sensor in loc["sensors"]:
                if sensor["parameter"] not in AQI_PARAMETERS:
                    continue
                fetched = api.measurements(sensor["id"], day)
                _write(
                    folder,
                    entries,
                    fetched,
                    source="api",
                    endpoint="measurements",
                    day=day,
                    location_id=loc["location_id"],
                    sensor_id=sensor["id"],
                    filename=f"measurements-{loc['location_id']}-{sensor['id']}.json",
                )
    else:
        raise ValueError(f"unknown EXTRACT_SOURCE {source!r} (api | archive | auto | sample)")

    manifest = {"day": day.isoformat(), "source": source, "entries": [asdict(e) for e in entries]}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return {"day": day.isoformat(), "source": source, "payloads": len(entries)}
