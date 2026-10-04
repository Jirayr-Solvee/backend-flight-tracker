"""Curated ACARS read model. No provider credentials or raw response storage.

An explicit refresh job writes a small redacted cache. API workers only read it:
opening the app cannot fan out provider calls or exhaust an account quota.
"""
import hashlib
import base64
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cockpit_importance import TOP_WINDOW_HOURS, importance_key, with_tier

RETENTION = timedelta(days=7)
SORTS = ('latest', 'top')


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
    connection.execute("CREATE INDEX IF NOT EXISTS cockpit_story_order ON cockpit_stories(received DESC,id DESC)")
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


def decode_cursor(cursor):
    if not cursor:return None
    try:
        value=json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor)%4)))
        if not isinstance(value,list) or len(value)!=2 or not all(isinstance(v,str) for v in value):raise ValueError()
        if not parse_time(value[0]) or len(value[1])>128 or not value[1]:raise ValueError()
        return value
    except Exception:
        raise ValueError('Invalid message cursor') from None


def read_stories(path, registration=None, now=None, flight=None, *, limit=50, category=None, cursor=None, sort='latest'):
    boundary=decode_cursor(cursor)
    if not 1<=limit<=50:raise ValueError('Invalid page size')
    if sort not in SORTS:raise ValueError('Invalid sort')
    now = now or datetime.now(timezone.utc)
    if not Path(path).is_file():
        return None
    connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=5)
    try:
        meta = connection.execute("SELECT value FROM cockpit_metadata WHERE key='updated_at'").fetchone()
        updated = parse_time(meta[0]) if meta else None
        if updated is None or now - updated > timedelta(hours=2):
            return None
        if sort == 'top' and not flight:
            return {"stories": top_stories(connection, now, registration, category, limit),
                    "updatedAt": utc_string(updated), "nextCursor": None}
        query = "SELECT id,received,payload FROM cockpit_stories WHERE received >= ? AND received <= ?"
        params = [utc_string(now - RETENTION), utc_string(now + timedelta(minutes=1))]
        if flight:
            query += " AND received >= ? AND received <= ? AND REPLACE(registration, '-', '') = ?"
            from .cockpit_tracking import tail
            params.extend([utc_string(flight['start']),utc_string(flight['end']),tail(flight['registration'])])
        elif registration:
            query += " AND registration = ?"
            params.append(registration.upper())
        if category:
            query += " AND json_extract(payload, '$.category') = ?"
            params.append(category)
        if boundary:
            query += " AND (received < ? OR (received = ? AND id < ?))"
            params.extend([boundary[0],boundary[0],boundary[1]])
        rows = connection.execute(query + " ORDER BY received DESC,id DESC" + ("" if flight else " LIMIT ?"), params+([] if flight else [limit+1]))
        stories=[]
        positions=[]
        for row in rows:
            story=with_tier(json.loads(row[2]))
            if flight:
                from .cockpit_tracking import matches
                if not matches(story,flight):continue
            stories.append(story)
            positions.append([row[1],row[0]])
            if len(stories)>limit:break
        next_cursor=None
        if len(stories)>limit:
            next_cursor=base64.urlsafe_b64encode(json.dumps(positions[limit-1]).encode()).decode().rstrip('=')
        return {"stories": stories[:limit], "updatedAt": utc_string(updated), "nextCursor":next_cursor}
    finally:
        connection.close()


def top_stories(connection, now, registration=None, category=None, limit=20):
    """Major and notable stories from the last few days, most important first.
    One bounded page: importance order has no stable cursor."""
    query = "SELECT payload FROM cockpit_stories WHERE received >= ? AND received <= ?"
    params = [utc_string(now - timedelta(hours=TOP_WINDOW_HOURS)), utc_string(now + timedelta(minutes=1))]
    if registration:
        query += " AND registration = ?"
        params.append(registration.upper())
    if category:
        query += " AND json_extract(payload, '$.category') = ?"
        params.append(category)
    stories = [with_tier(json.loads(row[0])) for row in connection.execute(query, params)]
    stories = [story for story in stories if story['tier'] != 'background']
    stories.sort(key=importance_key, reverse=True)
    return stories[:limit]
