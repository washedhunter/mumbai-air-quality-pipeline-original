"""All settings come from environment variables (see .env.example). Defaults suit local Docker."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

PACKAGE_DIR = Path(__file__).resolve().parent


def _resource_dir(name: str) -> Path:
    """sql/ and config/ live at the repo root in a checkout and inside the package once installed."""
    installed = PACKAGE_DIR / name
    if installed.is_dir():
        return installed
    return PACKAGE_DIR.parent.parent / name


SQL_DIR = _resource_dir("sql")
CONFIG_DIR = _resource_dir("config")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    warehouse_dsn: str = field(
        default_factory=lambda: _env(
            "WAREHOUSE_DSN", "postgresql://aq:aq@localhost:5433/air_quality"
        )
    )
    openaq_api_key: str = field(default_factory=lambda: _env("OPENAQ_API_KEY", ""))
    openaq_base_url: str = field(
        default_factory=lambda: _env("OPENAQ_BASE_URL", "https://api.openaq.org/v3")
    )
    archive_base_url: str = field(
        default_factory=lambda: _env(
            "OPENAQ_ARCHIVE_URL", "https://openaq-data-archive.s3.amazonaws.com"
        )
    )
    # Search area: OpenAQ's maximum radius (25 km) around central Mumbai.
    center_lat: float = field(default_factory=lambda: float(_env("AQ_CENTER_LAT", "19.0760")))
    center_lon: float = field(default_factory=lambda: float(_env("AQ_CENTER_LON", "72.8777")))
    radius_m: int = field(default_factory=lambda: int(_env("AQ_RADIUS_M", "25000")))
    # api | archive | auto | sample
    extract_source: str = field(default_factory=lambda: _env("EXTRACT_SOURCE", "auto"))
    # In `auto`, days at least this old come from the S3 archive (it lags ~4 days behind real time).
    archive_min_age_days: int = field(
        default_factory=lambda: int(_env("ARCHIVE_MIN_AGE_DAYS", "6"))
    )
    sample_dir: Path = field(
        default_factory=lambda: Path(_env("SAMPLE_DIR", str(_resource_dir("data") / "sample")))
    )
    landing_dir: Path = field(
        default_factory=lambda: Path(_env("LANDING_DIR", str(Path("data") / "landing")))
    )
    # How OpenAQ's "ppb" label on CPCB gases is read: cpcb_native (µg/m³, CO mg/m³) or true_ppb.
    ppb_label_means: str = field(default_factory=lambda: _env("PPB_LABEL_MEANS", "cpcb_native"))
    forecast_horizon_days: int = field(
        default_factory=lambda: int(_env("FORECAST_HORIZON_DAYS", "7"))
    )
    forecast_min_days_city: int = field(
        default_factory=lambda: int(_env("FORECAST_MIN_DAYS_CITY", "60"))
    )
    forecast_min_days_station: int = field(
        default_factory=lambda: int(_env("FORECAST_MIN_DAYS_STATION", "180"))
    )
    backtest_weeks: int = field(default_factory=lambda: int(_env("BACKTEST_WEEKS", "26")))
    backtest_station_origins: int = field(
        default_factory=lambda: int(_env("BACKTEST_STATION_ORIGINS", "6"))
    )
    http_timeout_s: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_S", "60")))
    http_max_retries: int = field(default_factory=lambda: int(_env("HTTP_MAX_RETRIES", "5")))
    stations_file: Path = field(
        default_factory=lambda: Path(_env("STATIONS_FILE", str(CONFIG_DIR / "stations.csv")))
    )
    always_forecast: bool = field(default_factory=lambda: _env_bool("ALWAYS_FORECAST", False))

    def source_for(self, day: date, today: date) -> str:
        """Which source a given day is pulled from."""
        if self.extract_source != "auto":
            return self.extract_source
        return "archive" if (today - day).days >= self.archive_min_age_days else "api"


def get_settings() -> Settings:
    return Settings()
