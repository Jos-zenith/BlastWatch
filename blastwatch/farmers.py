"""Farmer subscriptions: stage-targeted alerts, check-ins and officer-verified reports.

Each refresh, during send hours ([farmers] in risk_rules.toml):
1. A subscriber gets an alert when their district meets the alert rule ([alerts]), the
   forecast has not expired, their crop is in a susceptible stage, and they had no delivered
   alert within the cooldown. The advice follows the stage: leaf checks before heading,
   panicle protection at heading-flowering.
2. Each alert schedules check-in questions. A sample of non-alerted farmers in susceptible
   stages is asked too, so outbreaks the model missed become visible.
3. A farmer's YES is only a report. An officer verifies it, and the verdict becomes an
   Observation for calibration: present if confirmed, absent if the officer found no blast.

Every message is complete in the subscriber's language. The Tamil texts were drafted for this
prototype and must be reviewed by a Tamil-speaking agronomist before they reach farmers.
Fungicide names and doses are deliberately absent until TNAU reviews them.
"""
import hashlib
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import blocks, notify
from .alerts import format_dates, upcoming_high_days
from .models import District, FarmerMessage, Followup, Observation, Subscriber
from .risk import load_rules
from .runs import freshness
from .stage import CropVariety, Stage, crop_stage, load_crop_calendar

CONSENT_VERSION = "v1-draft"
# <SPONSOR> must name the body running the pilot (department, TNAU or KVK) before enrolment starts.
CONSENT_TEXT = {
    "en": "BlastWatch is a pilot run by <SPONSOR>. We will send you weather-based rice blast warnings "
          "and ask whether you saw symptoms. We store your phone number or chat id, district, variety and "
          "sowing or transplanting date only for this. Reply STOP to leave. Warnings are advice, not a diagnosis.",
    "ta": "BlastWatch என்பது <SPONSOR> நடத்தும் ஒரு சோதனைத் திட்டம். வானிலை அடிப்படையிலான நெல் குலை நோய் "
          "எச்சரிக்கைகளை அனுப்புவோம்; நோய் அறிகுறிகள் கண்டீர்களா என்றும் கேட்போம். இதற்காக மட்டுமே உங்கள் "
          "தொலைபேசி எண், மாவட்டம், ரகம், விதைப்பு அல்லது நடவு தேதியைச் சேமிப்போம். விலக STOP என பதில் "
          "அனுப்பவும். இவை ஆலோசனைகள் மட்டுமே, நோய் கண்டறிதல் அல்ல.",
}

ADVICE_FOR_STAGE = {"vegetative": "leaf", "booting": "leaf", "heading": "neck", "unknown": "any"}
ALERT_TEXT = {
    "en": {
        "head": "BlastWatch {name}: weather favours rice blast on {when}.",
        "leaf": "Check leaves for eye-shaped spots with grey centres. Do not add extra urea now. "
                "Spray only if spots are found, as advised by your agri officer.",
        "neck": "Your crop is flowering, when blast can attack the panicle neck. "
                "Contact your agri officer today about protecting the panicles.",
        "any": "Check leaves for eye-shaped spots with grey centres. If your crop is flowering, contact "
               "your agri officer today about protecting the panicles. Spray only on your agri officer's advice.",
        "foot": "Weather-based warning, not a diagnosis.",
    },
    "ta": {
        "head": "{name}: {when} நெல் குலை நோய்க்கு சாதகமான வானிலை.",
        "leaf": "இலைகளில் சாம்பல் நிற நடுப்பகுதியுடன் கண் வடிவப் புள்ளிகள் உள்ளதா என வயலைப் பார்வையிடவும். "
                "இப்போது கூடுதல் யூரியா இட வேண்டாம். புள்ளிகள் இருந்தால் மட்டும் வேளாண் அலுவலர் ஆலோசனைப்படி "
                "மருந்து தெளிக்கவும்.",
        "neck": "உங்கள் பயிர் பூக்கும் பருவத்தில் உள்ளது; இப்போது குலை நோய் கதிர்க் கழுத்தைத் தாக்கலாம். "
                "கதிர்களைப் பாதுகாப்பது குறித்து இன்றே வேளாண் அலுவலரை அணுகவும்.",
        "any": "இலைகளில் சாம்பல் நிற நடுப்பகுதியுடன் கண் வடிவப் புள்ளிகள் உள்ளதா என வயலைப் பார்வையிடவும். "
               "பயிர் பூக்கும் பருவத்தில் இருந்தால், கதிர்களைப் பாதுகாப்பது குறித்து இன்றே வேளாண் அலுவலரை "
               "அணுகவும். வேளாண் அலுவலர் ஆலோசனைப்படி மட்டுமே மருந்து தெளிக்கவும்.",
        "foot": "இது வானிலை அடிப்படையிலான எச்சரிக்கை, நோய் கண்டறிதல் அல்ல.",
    },
}
CHECKIN_TEXT = {
    "en": "BlastWatch check-in: in the last 7 days, have you seen eye-shaped spots with grey centres on the "
          "leaves, or a blackened neck below the panicle? Reply YES, NO or NOT SURE.",
    "ta": "BlastWatch: கடந்த 7 நாட்களில் இலைகளில் சாம்பல் நிற நடுப்பகுதியுடன் கண் வடிவப் புள்ளிகள் அல்லது "
          "கதிருக்குக் கீழே கழுத்தில் கருமை கண்டீர்களா? ஆம், இல்லை அல்லது தெரியவில்லை என பதில் அனுப்பவும்.",
}

Sender = Callable[[str, str, str], str]


def compose_alert(district: District, stage: str, high_days: list[date], language: str, block=None) -> str:
    """`block`: the farmer's block when its own risk raised the alert; the message then names it."""
    t = ALERT_TEXT[language]
    place = block or district
    name = (place.name_ta or place.name) if language == "ta" else place.name
    return " ".join([t["head"].format(name=name, when=format_dates(high_days, language)),
                     t[ADVICE_FOR_STAGE[stage]], t["foot"]])


def stage_of(sub: Subscriber, today: date, rules: dict, calendar: dict[str, CropVariety]) -> Stage:
    return crop_stage(sub.variety, sub.method, sub.establish_date, today, rules, calendar)


def _sampled(subscriber_id: int, day: date, fraction: float) -> bool:
    """Deterministic per subscriber and day, so repeated refreshes on one day agree."""
    return hashlib.sha256(f"{subscriber_id}:{day}".encode()).digest()[0] / 256 < fraction


def _add_followup(session: Session, subscriber_id: int, due_on: date, origin: str,
                  alert_message_id: int | None) -> None:
    exists = session.scalar(select(Followup.id).where(Followup.subscriber_id == subscriber_id,
                                                      Followup.due_on == due_on))
    if not exists:
        session.add(Followup(subscriber_id=subscriber_id, due_on=due_on, origin=origin,
                             alert_message_id=alert_message_id))


def _deliver(session: Session, sub: Subscriber, kind: str, body: str, stage: str | None,
             now: datetime, send: Sender) -> FarmerMessage:
    status = send(sub.channel, sub.contact, body)
    msg = FarmerMessage(subscriber_id=sub.id, kind=kind, stage=stage, body=body, status=status,
                        created_at=now, sent_at=now if status == "sent" else None)
    session.add(msg)
    session.flush()
    return msg


def notify_farmers(session: Session, rules: dict, now: datetime | None = None,
                   send: Sender = notify.send) -> dict:
    now = now or datetime.now()
    today = now.date()
    f, a = rules["farmers"], rules["alerts"]
    start_hour, end_hour = f["send_hours"]
    if not start_hour <= now.hour < end_hour:
        return {"alerts": 0, "checkins": 0, "skipped": "outside send hours"}

    expired = freshness(session, rules)["status"] == "expired"
    calendar = load_crop_calendar()
    subs = session.scalars(select(Subscriber).where(Subscriber.active).order_by(Subscriber.id)).all()
    districts = {d.id: d for d in session.scalars(select(District))}
    high: dict[int, list[date]] = {}
    for district_id in {s.district_id for s in subs}:
        days = [] if expired else upcoming_high_days(session, district_id, rules, today)
        high[district_id] = days if len(days) >= a["min_high_days"] else []
    # A farmer in a known block is alerted on that block's risk (one point, closer to the field);
    # otherwise, or while block data is stale, on the district's.
    use_blocks = not expired and blocks.fresh(session, rules, now)
    sub_high: dict[int, list[date]] = {}
    sub_block = {}
    for sub in subs:
        block = sub_block[sub.id] = blocks.resolve(session, sub.district_id, sub.block) if use_blocks else None
        if block is None:
            sub_high[sub.id] = high[sub.district_id]
        else:
            days = blocks.upcoming_high_days(session, block.id, rules, today)
            sub_high[sub.id] = days if len(days) >= a["min_high_days"] else []

    cooldown_start = now - timedelta(days=a["cooldown_days"])
    alerts = 0
    for sub in subs:
        stage = stage_of(sub, today, rules, calendar)
        if not stage.susceptible:
            continue
        if sub_high[sub.id]:
            # Failed deliveries do not count, so a later refresh retries them.
            recent = session.scalar(select(FarmerMessage.id).where(
                FarmerMessage.subscriber_id == sub.id, FarmerMessage.kind == "alert",
                FarmerMessage.created_at >= cooldown_start, FarmerMessage.status.not_like("failed%"),
            ).limit(1))
            if recent:
                continue
            body = compose_alert(districts[sub.district_id], stage.name, sub_high[sub.id], sub.language,
                                 sub_block[sub.id])
            msg = _deliver(session, sub, "alert", body, stage.name, now, send)
            if msg.status.startswith("failed"):
                continue
            for d in f["checkin_days"]:
                _add_followup(session, sub.id, today + timedelta(days=d), "alert", msg.id)
            alerts += 1
        elif not expired and _sampled(sub.id, today, f["no_alert_checkin_weekly_fraction"] / 7):
            _add_followup(session, sub.id, today + timedelta(days=7), "no_alert_sample", None)
    session.commit()
    return {"alerts": alerts, "checkins": send_due_checkins(session, rules, now, send), "skipped": None}


def send_due_checkins(session: Session, rules: dict, now: datetime, send: Sender = notify.send) -> int:
    today = now.date()
    answer_days = rules["farmers"]["checkin_answer_days"]
    due = session.execute(
        select(Followup, Subscriber).join(Subscriber, Subscriber.id == Followup.subscriber_id)
        .where(Followup.asked_at.is_(None), Subscriber.active,
               # A question about "the last 7 days" is meaningless long after it was due.
               Followup.due_on <= today, Followup.due_on > today - timedelta(days=answer_days))
        .order_by(Followup.due_on)
    ).all()
    sent = 0
    for followup, sub in due:
        # One open question per farmer; it stops blocking once it is older than answer_days.
        open_question = session.scalar(select(Followup.id).where(
            Followup.subscriber_id == sub.id, Followup.answered_at.is_(None),
            Followup.asked_at >= now - timedelta(days=answer_days),
        ).limit(1))
        if open_question:
            continue
        msg = _deliver(session, sub, "checkin", CHECKIN_TEXT[sub.language], None, now, send)
        if msg.status.startswith("failed"):
            continue
        followup.asked_at = now
        session.flush()
        sent += 1
    session.commit()
    return sent


def record_reply(session: Session, contact: str, answer: str, now: datetime | None = None) -> Followup | None:
    """Attach a farmer's answer to their latest open question. None if there is none."""
    followup = session.scalars(
        select(Followup).join(Subscriber, Subscriber.id == Followup.subscriber_id)
        .where(Subscriber.contact == contact, Followup.asked_at.is_not(None), Followup.answered_at.is_(None))
        .order_by(Followup.asked_at.desc())
    ).first()
    if followup is None:
        return None
    followup.answer, followup.answered_at = answer, now or datetime.now()
    session.commit()
    return followup


def verify_report(session: Session, followup_id: int, status: str, note: str = "",
                  now: datetime | None = None, rules: dict | None = None) -> Followup:
    """Officer verdict on a YES/NOT SURE report; it becomes a calibration Observation, carrying the
    farmer's block, variety and the crop stage on the day they answered.

    A rejected report (e.g. brown spot, not blast) is recorded as an absence in that field.
    """
    followup = session.get(Followup, followup_id)
    if followup is None or followup.answer not in ("yes", "unsure"):
        raise LookupError("no farmer report with that id")
    sub = session.get(Subscriber, followup.subscriber_id)
    followup.officer_status, followup.officer_note = status, note or None
    followup.verified_at = now or datetime.now()
    source = f"farmer-report:{followup.id}"
    obs = session.scalar(select(Observation).where(Observation.source == source))
    if obs is None:
        obs = Observation(district_id=sub.district_id, date=followup.answered_at.date(), source=source)
        session.add(obs)
    obs.blast_present, obs.note = status == "confirmed", note or None
    stage = stage_of(sub, followup.answered_at.date(), rules or load_rules(), load_crop_calendar()).name
    obs.block, obs.variety = sub.block, sub.variety
    obs.crop_stage = stage if stage in ("vegetative", "booting", "heading", "ripening") else None
    session.commit()
    return followup
