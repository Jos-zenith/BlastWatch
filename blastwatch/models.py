"""Database tables."""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class District(Base):
    __tablename__ = "district"
    __table_args__ = (UniqueConstraint("name", "state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    name_ta: Mapped[str | None] = mapped_column(String(80))  # Tamil name for farmer messages
    state: Mapped[str] = mapped_column(String(80))
    # Point used for weather queries (district headquarters).
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)


class WeatherHourly(Base):
    __tablename__ = "weather_hourly"
    __table_args__ = (UniqueConstraint("district_id", "ts", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)  # naive local time (config.TIMEZONE)
    temp_c: Mapped[float | None] = mapped_column(Float)
    rh_pct: Mapped[float | None] = mapped_column(Float)
    precip_mm: Mapped[float | None] = mapped_column(Float)
    cloud_pct: Mapped[float | None] = mapped_column(Float)
    dew_point_c: Mapped[float | None] = mapped_column(Float)
    # Model estimate (Open-Meteo), 0-100 %.
    leaf_wet_prob: Mapped[float | None] = mapped_column(Float)
    # Measured by a leaf-wetness sensor: wet minutes within the hour.
    leaf_wet_min: Mapped[float | None] = mapped_column(Float)
    # "open-meteo" | "met-no" | "nasa-power" | "sensor:<station>"
    source: Mapped[str] = mapped_column(String(20))
    is_forecast: Mapped[bool] = mapped_column(default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class RiskDaily(Base):
    __tablename__ = "risk_daily"
    __table_args__ = (UniqueConstraint("district_id", "date", "model_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    score: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(10))
    wet_hours: Mapped[int] = mapped_column(Integer)
    longest_wet_run: Mapped[int] = mapped_column(Integer)
    rain_mm: Mapped[float] = mapped_column(Float)
    mean_temp_c: Mapped[float | None] = mapped_column(Float)
    susceptibility: Mapped[float] = mapped_column(Float)
    is_forecast: Mapped[bool] = mapped_column(default=False)
    mean_cloud_pct: Mapped[float | None] = mapped_column(Float)
    # Which leaf-wetness signal dominated the window: sensor | lwp | dpd | rh.
    wetness_basis: Mapped[str | None] = mapped_column(String(10))
    model_version: Mapped[str] = mapped_column(String(20))
    computed_at: Mapped[datetime] = mapped_column(DateTime)


class EnsembleDaily(Base):
    """Risk spread across ensemble members for one district, night and model (see ensemble.py).
    model is "ecmwf" | "gefs" | "icon", or "all" for the equal-weight pool of the three."""
    __tablename__ = "ensemble_daily"
    __table_args__ = (UniqueConstraint("district_id", "date", "model", "model_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    model: Mapped[str] = mapped_column(String(10))
    members: Mapped[int] = mapped_column(Integer)
    p_high: Mapped[float] = mapped_column(Float)
    p_moderate: Mapped[float] = mapped_column(Float)
    score_p10: Mapped[float] = mapped_column(Float)
    score_p50: Mapped[float] = mapped_column(Float)
    score_p90: Mapped[float] = mapped_column(Float)
    score_mean: Mapped[float] = mapped_column(Float)
    wet_hours_p50: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(20))
    run_at: Mapped[datetime] = mapped_column(DateTime)  # when the members were fetched


class Block(Base):
    """A development block (panchayat union), located at its headquarters town.
    Built by scripts/build_block_seed.py; located_by says how the point was found."""
    __tablename__ = "block"
    __table_args__ = (UniqueConstraint("district_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    name_ta: Mapped[str | None] = mapped_column(String(80))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    located_by: Mapped[str | None] = mapped_column(String(12))  # osm-ta | osm-en | nominatim | wikidata
    hq_place: Mapped[str | None] = mapped_column(String(80))
    wikidata: Mapped[str | None] = mapped_column(String(16))


class BlockWeatherHourly(Base):
    """Forecast weather at a block's headquarters (same variables as WeatherHourly)."""
    __tablename__ = "block_weather_hourly"
    __table_args__ = (UniqueConstraint("block_id", "ts", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("block.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    temp_c: Mapped[float | None] = mapped_column(Float)
    rh_pct: Mapped[float | None] = mapped_column(Float)
    precip_mm: Mapped[float | None] = mapped_column(Float)
    cloud_pct: Mapped[float | None] = mapped_column(Float)
    dew_point_c: Mapped[float | None] = mapped_column(Float)
    leaf_wet_prob: Mapped[float | None] = mapped_column(Float)
    leaf_wet_min: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))
    is_forecast: Mapped[bool] = mapped_column(default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class BlockRiskDaily(Base):
    __tablename__ = "block_risk_daily"
    __table_args__ = (UniqueConstraint("block_id", "date", "model_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("block.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    score: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(10))
    wet_hours: Mapped[int] = mapped_column(Integer)
    longest_wet_run: Mapped[int] = mapped_column(Integer)
    rain_mm: Mapped[float] = mapped_column(Float)
    wetness_basis: Mapped[str | None] = mapped_column(String(10))
    is_forecast: Mapped[bool] = mapped_column(default=False)
    model_version: Mapped[str] = mapped_column(String(20))
    computed_at: Mapped[datetime] = mapped_column(DateTime)


class Production(Base):
    __tablename__ = "production"
    __table_args__ = (UniqueConstraint("area_code", "item_code", "year", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    area_code: Mapped[int] = mapped_column(Integer)
    area: Mapped[str] = mapped_column(String(120), index=True)
    item_code: Mapped[int] = mapped_column(Integer)
    item: Mapped[str] = mapped_column(String(80))
    year: Mapped[int] = mapped_column(Integer)
    area_ha: Mapped[float | None] = mapped_column(Float)
    production_t: Mapped[float | None] = mapped_column(Float)
    yield_kg_ha: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))


class Gene(Base):
    __tablename__ = "gene"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), unique=True)
    organism: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(40))  # "host-resistance" | "pathogen-avirulence"
    description: Mapped[str] = mapped_column(Text)
    ncbi_query: Mapped[str] = mapped_column(Text)
    ncbi_hits: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)


class GeneRecord(Base):
    __tablename__ = "gene_record"
    __table_args__ = (UniqueConstraint("gene_id", "accession"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gene_id: Mapped[int] = mapped_column(ForeignKey("gene.id"), index=True)
    accession: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    length: Mapped[int | None] = mapped_column(Integer)
    organism: Mapped[str | None] = mapped_column(String(120))
    update_date: Mapped[str | None] = mapped_column(String(20))


class Variety(Base):
    __tablename__ = "variety"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    genes: Mapped[str] = mapped_column(String(120))  # ";"-separated gene symbols
    states: Mapped[str] = mapped_column(Text)  # ";"-separated recommended states
    status: Mapped[str] = mapped_column(String(40))  # "released" | "breeding line"
    note: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)


class IngestRun(Base):
    """One attempt to pull data from an external source; drives freshness checks."""
    __tablename__ = "ingest_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10))  # "ok" | "failed"
    rows: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Observation(Base):
    """Field-confirmed presence/absence of blast, used to calibrate the model."""
    __tablename__ = "observation"
    __table_args__ = (UniqueConstraint("district_id", "date", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    blast_present: Mapped[bool] = mapped_column(Boolean)
    severity: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)


class Alert(Base):
    """A farmer-facing message prepared for one district and one risk episode."""
    __tablename__ = "alert"
    __table_args__ = (UniqueConstraint("district_id", "episode_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    episode_start: Mapped[date] = mapped_column(Date)
    high_days: Mapped[str] = mapped_column(String(80))  # ";"-separated ISO dates
    message_en: Mapped[str] = mapped_column(Text)
    message_ta: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12))  # "ready" | "sent" | "expired"
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # When the message text was last generated from the risk rows (refreshed while "ready").
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class FieldCheck(Base):
    """An officer's field visit: blast found or not. Each one becomes an Observation, so every
    alert (and every routine visit) adds a labelled data point for calibration."""
    __tablename__ = "field_check"

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alert.id"), index=True)  # None = routine visit
    checked_on: Mapped[date] = mapped_column(Date)
    blast_found: Mapped[bool] = mapped_column(Boolean)
    fields_checked: Mapped[int | None] = mapped_column(Integer)
    block: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Subscriber(Base):
    """A farmer who consented to alerts and check-ins, enrolled by an officer or operator."""
    __tablename__ = "subscriber"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact: Mapped[str] = mapped_column(String(64), unique=True)  # phone number or Telegram chat id
    channel: Mapped[str] = mapped_column(String(10))  # "outbox" | "telegram"
    district_id: Mapped[int] = mapped_column(ForeignKey("district.id"), index=True)
    block: Mapped[str | None] = mapped_column(String(80))  # for officer visits; risk is per district
    variety: Mapped[str] = mapped_column(String(80))  # as the farmer named it
    method: Mapped[str] = mapped_column(String(16))  # "transplanted" | "direct_seeded"
    establish_date: Mapped[date] = mapped_column(Date)  # transplanting or sowing date
    language: Mapped[str] = mapped_column(String(2))  # "ta" | "en"
    consent_at: Mapped[datetime] = mapped_column(DateTime)
    consent_version: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class FarmerMessage(Base):
    """One message to one subscriber: an alert or a check-in question."""
    __tablename__ = "farmer_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscriber.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # "alert" | "checkin"
    stage: Mapped[str | None] = mapped_column(String(12))  # crop stage the alert was worded for
    body: Mapped[str] = mapped_column(Text)
    # "queued" (waiting for an officer to forward) | "sent" | "failed:<reason>"
    status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class Followup(Base):
    """A check-in question: did the farmer see blast symptoms? Verified by an officer."""
    __tablename__ = "followup"
    __table_args__ = (UniqueConstraint("subscriber_id", "due_on"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscriber.id"), index=True)
    due_on: Mapped[date] = mapped_column(Date)
    origin: Mapped[str] = mapped_column(String(16))  # "alert" | "no_alert_sample"
    alert_message_id: Mapped[int | None] = mapped_column(ForeignKey("farmer_message.id"))
    asked_at: Mapped[datetime | None] = mapped_column(DateTime)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    answer: Mapped[str | None] = mapped_column(String(8))  # "yes" | "no" | "unsure"
    officer_status: Mapped[str] = mapped_column(String(12), default="unverified")  # | "confirmed" | "rejected"
    officer_note: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
