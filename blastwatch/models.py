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
    # Rice area from state crop statistics; optional until that dataset is loaded.
    rice_area_ha: Mapped[float | None] = mapped_column(Float)


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
    # Which leaf-wetness signal dominated the window: sensor | lwp | dpd | rh.
    wetness_basis: Mapped[str | None] = mapped_column(String(10))
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
    status: Mapped[str] = mapped_column(String(12))  # "ready" | "sent"
    created_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
