"""Database tables."""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class District(Base):
    __tablename__ = "district"
    __table_args__ = (UniqueConstraint("name", "state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
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
    source: Mapped[str] = mapped_column(String(20))  # "open-meteo" | "nasa-power"
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
