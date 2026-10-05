"""Farmer alerts: when to raise one, and the short bilingual message officers forward.

An alert is raised only for sustained, near-term risk on fresh data (see [alerts] and
[freshness] in risk_rules.toml), and at most once per risk episode per district. Alerts are
stored as "ready" for an officer to forward by SMS/WhatsApp; no message leaves the system
automatically until a delivery provider is wired in.

The Tamil text was drafted for this prototype and should be reviewed by a Tamil-speaking
agronomist (e.g. TNAU/KVK extension staff) before it reaches farmers.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Alert, District, RiskDaily
from .runs import freshness


def _dates(days: list[date]) -> str:
    return ", ".join(d.strftime("%d/%m") for d in days)


def compose(district: District, high_days: list[date]) -> tuple[str, str]:
    """Crop stage is not known per field, so the message covers both leaf blast and, for
    fields at flowering, neck blast (which leaf checks miss and which costs the most yield)."""
    when = _dates(high_days)
    en = (f"BlastWatch {district.name}: high rice blast risk on {when}. Check leaves for "
          f"eye-shaped spots. If the crop is flowering, also check the panicle neck for dark rot. "
          f"Spray only if spots or neck rot are found, as advised by your agri officer.")
    # The panicle-neck sentence was added after the rest of the Tamil draft; review it with it.
    ta = (f"{district.name_ta or district.name}: {when} நெல் குலை நோய் அபாயம் அதிகம். "
          f"இலைகளில் கண் வடிவப் புள்ளிகள் உள்ளதா என வயலைப் பார்வையிடவும். பயிர் பூக்கும் "
          f"பருவத்தில் இருந்தால், கதிர்க் கழுத்து கருமையாக அழுகியுள்ளதா எனவும் பார்க்கவும். "
          f"புள்ளிகள் அல்லது கழுத்து அழுகல் இருந்தால் மட்டும் வேளாண் அலுவலர் ஆலோசனைப்படி "
          f"மருந்து தெளிக்கவும்.")
    return en, ta


def upcoming_high_days(session: Session, district_id: int, rules: dict, today: date) -> list[date]:
    horizon = rules["alerts"]["horizon_days"]
    return list(session.scalars(
        select(RiskDaily.date).where(
            RiskDaily.district_id == district_id,
            RiskDaily.model_version == rules["model_version"],
            RiskDaily.level == "High",
            RiskDaily.date >= today,
            RiskDaily.date < today + timedelta(days=horizon),
        ).order_by(RiskDaily.date)
    ))


def evaluate(session: Session, rules: dict, today: date | None = None) -> dict:
    """Create "ready" alerts for districts that meet the rule. Returns what happened."""
    today = today or date.today()
    fresh = freshness(session, rules)
    if fresh["status"] == "expired":
        return {"created": [], "suppressed": f"forecast data {fresh['status']}"}

    a = rules["alerts"]
    created = []
    for district in session.scalars(select(District).order_by(District.id)).all():
        high_days = upcoming_high_days(session, district.id, rules, today)
        if len(high_days) < a["min_high_days"]:
            continue
        start = high_days[0]
        recent = session.scalar(
            select(Alert.id).where(
                Alert.district_id == district.id,
                Alert.episode_start >= start - timedelta(days=a["cooldown_days"]),
            ).limit(1)
        )
        if recent:
            continue
        en, ta = compose(district, high_days)
        session.add(Alert(
            district_id=district.id, episode_start=start,
            high_days=";".join(d.isoformat() for d in high_days),
            message_en=en, message_ta=ta, status="ready", created_at=datetime.now(),
        ))
        created.append(district.name)
    session.commit()
    return {"created": created, "suppressed": None}
