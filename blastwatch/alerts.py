"""Farmer alerts: when to raise one, and the short bilingual message officers forward.

An alert is raised only for sustained, near-term risk on fresh data (see [alerts] and
[freshness] in risk_rules.toml), and at most once per risk episode per district. Alerts are
stored as "ready" for an officer to forward by SMS/WhatsApp; no message leaves the system
automatically until a delivery provider is wired in.

A "ready" message is rewritten on every refresh from the same risk rows the officer view shows,
so its days always match the strip. When its district no longer meets the alert rule (the forecast
eased, the rules changed, or its days passed) it becomes "expired", so a stale message is never
offered for forwarding; if the risk returns within the cooldown it is reissued. A "sent" message is
left as it was sent.

The Tamil text was drafted for this prototype and should be reviewed by a Tamil-speaking
agronomist (e.g. TNAU/KVK extension staff) before it reaches farmers.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Alert, District, RiskDaily
from .runs import freshness


WEEKDAYS = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "ta": ("திங்கள்", "செவ்வாய்", "புதன்", "வியாழன்", "வெள்ளி", "சனி", "ஞாயிறு"),
}
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_dates(days: list[date], language: str = "en") -> str:
    """Weekday plus date, so a forwarded message cannot be misread: "Wed 8 Oct" / "புதன் 8/10"."""
    if language == "ta":
        return ", ".join(f"{WEEKDAYS['ta'][d.weekday()]} {d.day}/{d.month}" for d in days)
    return ", ".join(f"{WEEKDAYS['en'][d.weekday()]} {d.day} {MONTHS_EN[d.month - 1]}" for d in days)


def place_names(district: District, block=None) -> tuple[str, str]:
    """"Madurai" / "மதுரை", or with a block "Madurai (Vadipatti block)" / "மதுரை (வாடிப்பட்டி வட்டாரம்)"."""
    d_ta = district.name_ta or district.name
    if block is None:
        return district.name, d_ta
    return f"{district.name} ({block.name} block)", f"{d_ta} ({block.name_ta or block.name} வட்டாரம்)"


def compose(district: District, high_days: list[date], block=None) -> tuple[str, str]:
    """Crop stage is not known per field, so the message covers both leaf blast and, for
    fields at flowering, neck blast (which leaf checks miss and which costs the most yield).
    With `block`, the message is for that block's WhatsApp group and names it."""
    when, when_ta = format_dates(high_days, "en"), format_dates(high_days, "ta")
    name_en, name_ta = place_names(district, block)
    en = (f"BlastWatch {name_en}: high rice blast risk on {when}. Check leaves for "
          f"eye-shaped spots. If the crop is flowering, also check the panicle neck for dark rot. "
          f"Spray only if spots or neck rot are found, as advised by your agri officer.")
    # The panicle-neck sentence was added after the rest of the Tamil draft; review it with it.
    ta = (f"{name_ta}: {when_ta} நெல் குலை நோய் அபாயம் அதிகம். "
          f"இலைகளில் கண் வடிவப் புள்ளிகள் உள்ளதா என வயலைப் பார்வையிடவும். பயிர் பூக்கும் "
          f"பருவத்தில் இருந்தால், கதிர்க் கழுத்து கருமையாக அழுகியுள்ளதா எனவும் பார்க்கவும். "
          f"புள்ளிகள் அல்லது கழுத்து அழுகல் இருந்தால் மட்டும் வேளாண் அலுவலர் ஆலோசனைப்படி "
          f"மருந்து தெளிக்கவும்.")
    return en, ta


def upcoming_high_days(session: Session, district_id: int, rules: dict, today: date) -> list[date]:
    """High days in the alert horizon under the configured district rule (see [alerts] district_rule)."""
    if rules["alerts"].get("district_rule", "hq") == "blocks_half":
        from . import blocks  # blocks imports this module

        days = blocks.share_high_days(session, district_id, rules, today)
        if days is not None:
            return days
    return hq_high_days(session, district_id, rules, today)


def hq_high_days(session: Session, district_id: int, rules: dict, today: date) -> list[date]:
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


def expire_unsupported(session: Session, today: date, alerting: set[int] | None = None) -> list[int]:
    """Withdraw unsent messages the current forecast no longer supports: every one whose district is
    not in `alerting`, or, when the forecast itself has expired (`alerting` None), every one whose
    days have all passed."""
    expired = []
    for alert in session.scalars(select(Alert).where(Alert.status == "ready")):
        if alerting is None:
            stale = all(date.fromisoformat(d) < today for d in alert.high_days.split(";"))
        else:
            stale = alert.district_id not in alerting
        if stale:
            alert.status = "expired"
            expired.append(alert.id)
    return expired


def evaluate(session: Session, rules: dict, today: date | None = None, now: datetime | None = None) -> dict:
    """Create or refresh "ready" alerts for districts that meet the rule. Returns what happened."""
    now = now or datetime.now()
    today = today or now.date()
    fresh = freshness(session, rules)
    if fresh["status"] == "expired":
        expired = expire_unsupported(session, today)
        session.commit()
        return {"created": [], "updated": [], "expired": expired,
                "suppressed": f"forecast data {fresh['status']}"}

    a = rules["alerts"]
    created, updated, alerting = [], [], set()
    for district in session.scalars(select(District).order_by(District.id)).all():
        high_days = upcoming_high_days(session, district.id, rules, today)
        if len(high_days) < a["min_high_days"]:
            continue
        alerting.add(district.id)
        start = high_days[0]
        recent = session.scalar(
            select(Alert).where(
                Alert.district_id == district.id,
                Alert.episode_start >= start - timedelta(days=a["cooldown_days"]),
            ).order_by(Alert.episode_start.desc()).limit(1)
        )
        joined = ";".join(d.isoformat() for d in high_days)
        en, ta = compose(district, high_days)
        if recent is not None:
            # Same episode: keep an unsent message in step with the latest forecast, and reissue
            # one that was withdrawn when the risk eased. A sent message starts the cooldown.
            if recent.status == "expired" or (recent.status == "ready" and recent.high_days != joined):
                recent.high_days, recent.message_en, recent.message_ta = joined, en, ta
                recent.status, recent.updated_at = "ready", now
                updated.append(district.name)
            continue
        session.add(Alert(
            district_id=district.id, episode_start=start, high_days=joined,
            message_en=en, message_ta=ta, status="ready", created_at=now, updated_at=now,
        ))
        created.append(district.name)
    expired = expire_unsupported(session, today, alerting)
    session.commit()
    return {"created": created, "updated": updated, "expired": expired, "suppressed": None}
