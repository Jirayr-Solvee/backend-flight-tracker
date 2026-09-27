"""Curated ACARS read model. No provider credentials or raw response storage.

An explicit refresh job writes a small redacted cache. API workers only read it:
opening the app cannot fan out provider calls or exhaust an account quota.
"""
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION = timedelta(days=7)


def parse_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def utc_string(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def curate_message(message, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = parse_time(message.get("timestamp"))
    if stamp is None or stamp < now - RETENTION or stamp > now + timedelta(minutes=1):
        return None
    raw = message.get("text")
    if not isinstance(raw, str) or not 12 <= len(raw) <= 2000:
        return None
    raw = re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)).strip()
    text = raw.upper()
    if re.search(r"\b(?:NOT|NO LONGER|IF|AVOID) DIVERTING\b", text):
        return None
    # Only publish known, bounded operational excerpts. Full raw messages can
    # contain passenger/crew names and phone numbers, even in otherwise useful reports.
    if "SMELL" in text and "OVEN OFF" in text and re.search(r"SMELL (?:SEEMS TO BE |IS )?GONE", text):
        title = "A smell in the galley.\nAn oven switched off."
        summary = "The transmission reports a smell that appeared to clear after an oven was switched off."
        category = "Cabin"
        pattern = r"(?:FWD GALLY HAD A SMELL|SMELL)[A-Z /]{0,90}OVEN OFF[A-Z /]{0,45}SMELL (?:SEEMS TO BE |IS )?GONE"
    elif "APU INOP" in text and "NEED GPU" in text:
        title = "A little help\non arrival."
        summary = "Ground power is requested because the aircraft's auxiliary power unit is unavailable."
        category = "Operations"
        pattern = r"NEED GPU(?: UPON ARRIVAL)?[ ./-]*APU INOP"
    elif "IFE PANEL HAS NOT COOLED DOWN" in text:
        title = "A panel that\nhasn't cooled down."
        summary = "The message reports that an entertainment panel had not cooled down after it was switched off."
        category = "Cabin"
        pattern = r"IFE PANEL HAS NOT COOLED DOWN(?: AT ALL)? AFTER TURNING IT OFF"
    elif re.search(r"DIVERTING TO [A-Z]{4} DUE FUEL", text):
        title = "A change of plan\nin the sky."
        destination = re.search(r"DIVERTING TO ([A-Z]{4}) DUE FUEL", text).group(1)
        summary = f"The transmission reports a fuel-related diversion to {destination}. It does not establish an emergency."
        category = "Diversion"
        pattern = r"DIVERTING TO [A-Z]{4} DUE FUEL"
    else:
        return None
    match = re.search(pattern, text)
    if not match:
        return None
    excerpt = match.group(0)
    # Restrict publication to aviation vocabulary, not arbitrary text captured
    # between the matched terms. Unknown wording waits for a reviewed rule.
    allowed_words = set("FWD GALLY HAD A SMELL WE TURMED TURNED THE OVEN OFF SEEMS TO BE IS GONE NEED GPU UPON ARRIVAL APU INOP IFE PANEL HAS NOT COOLED DOWN AT ALL AFTER TURNING IT DIVERTING DUE FUEL".split())
    for word in re.findall(r"[A-Z]+", excerpt):
        if word not in allowed_words and not (category == "Diversion" and word == destination):
            return None
    airframe = message.get("airframe")
    tail = message.get("tail") or (airframe.get("tail") if isinstance(airframe, dict) else None)
    if not isinstance(tail, str) or not re.fullmatch(r"[A-Za-z0-9-]{3,12}", tail):
        return None
    tail = tail.upper()
    flight = message.get("flightNumber")
    if not isinstance(flight, str) or not re.fullmatch(r"[A-Za-z0-9]{3,10}", flight):
        flight = None
    # A date bucket collapses multiple receivers of the same transmission.
    identity = hashlib.sha256(f"{tail}|{stamp.date()}|{excerpt}".encode()).hexdigest()[:24]
    latitude = longitude = None
    # Only attach a position explicitly supplied on the message. A mutable
    # flight object's current location is not this historical message's position.
    lat, lon = message.get("latitude"), message.get("longitude")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)):
        if abs(lat) <= 90 and abs(lon) <= 180:
            latitude, longitude = lat, lon
    return dict(id=identity, title=title, summary=summary, category=category,
                flight=flight.upper() if flight else None, registration=tail,
                receivedAt=utc_string(stamp), transmission=excerpt,
                latitude=latitude, longitude=longitude)


def open_store(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS cockpit_stories (id TEXT PRIMARY KEY, received TEXT NOT NULL, registration TEXT NOT NULL, payload TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS cockpit_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return connection


def save_messages(path, messages, now=None):
    now = now or datetime.now(timezone.utc)
    stories = [story for msg in messages if (story := curate_message(msg, now))]
    connection = open_store(path)
    try:
        with connection:
            connection.execute("DELETE FROM cockpit_stories WHERE received < ?", (utc_string(now - RETENTION),))
            for story in stories:
                connection.execute("INSERT INTO cockpit_stories VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                                   (story["id"], story["receivedAt"], story["registration"], json.dumps(story)))
            connection.execute("INSERT INTO cockpit_metadata VALUES ('updated_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (utc_string(now),))
        return len({story["id"] for story in stories})
    finally:
        connection.close()


def read_stories(path, registration=None, now=None):
    now = now or datetime.now(timezone.utc)
    if not Path(path).is_file():
        return None
    connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=5)
    try:
        meta = connection.execute("SELECT value FROM cockpit_metadata WHERE key='updated_at'").fetchone()
        updated = parse_time(meta[0]) if meta else None
        if updated is None or now - updated > timedelta(hours=2):
            return None
        query = "SELECT payload FROM cockpit_stories WHERE received >= ? AND received <= ?"
        params = [utc_string(now - RETENTION), utc_string(now + timedelta(minutes=1))]
        if registration:
            query += " AND registration = ?"
            params.append(registration.upper())
        rows = connection.execute(query + " ORDER BY received DESC LIMIT 50", params).fetchall()
        return {"stories": [json.loads(row[0]) for row in rows], "updatedAt": utc_string(updated)}
    finally:
        connection.close()
