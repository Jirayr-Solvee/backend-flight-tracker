"""Read-only tracked-flight targets and conservative message attribution."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .cockpit_stories import parse_time


def number(value):
    value = re.sub(r'\s+', '', str(value or '').upper())
    match = re.fullmatch(r'([A-Z0-9]{2,3}?)(\d+)([A-Z]?)', value)
    return match[1] + str(int(match[2])) + match[3] if match else value


def tail(value):
    return re.sub(r'[-\s]', '', str(value or '').upper())


def target(row):
    reg = row['aircraft_reg']
    if not reg or not re.fullmatch(r'[A-Za-z0-9-]{3,12}', reg):
        return None
    start = next((stamp for key in ('dep_runway','dep_revised','dep_scheduled')
                  if (stamp := parse_time(row[key]))), None)
    end = next((stamp for key in ('arr_runway','arr_revised','arr_scheduled')
                if (stamp := parse_time(row[key]))), None)
    if not start or not end or not start < end <= start + timedelta(hours=30):
        return None
    aliases = {number(row['number'])}
    for prefix in (row['iata'], row['icao']):
        raw = re.sub(r'\s+', '', str(row['number']).upper())
        if prefix and raw.startswith(prefix.upper()):
            suffix = raw[len(prefix):]
            aliases.update(number(p + suffix) for p in (row['iata'], row['icao']) if p)
    return dict(id=row['id'], registration=reg.upper(), icao=row['aircraft_modeS'],
                aliases=aliases, start=start-timedelta(hours=1), end=end+timedelta(minutes=30))


def load_targets(path='database.db', *, user_id=None, flight_id=None, now=None):
    """No ORM startup/table creation in the sandboxed ingestion worker."""
    now = now or datetime.now(timezone.utc)
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        query = '''SELECT f.id,f.number,f.aircraft_reg,f.aircraft_modeS,a.iata,a.icao,
          d.runway_time_utc dep_runway,d.revised_time_utc dep_revised,d.scheduled_time_utc dep_scheduled,
          r.runway_time_utc arr_runway,r.revised_time_utc arr_revised,r.scheduled_time_utc arr_scheduled
          FROM flight f LEFT JOIN departure d ON d.flight_id=f.id
          LEFT JOIN arrival r ON r.flight_id=f.id LEFT JOIN airline a ON a.id=f.airline_id
          WHERE EXISTS (SELECT 1 FROM userflightlink u WHERE u.flight_id=f.id'''
        args = []
        if user_id is not None:
            query += ' AND u.user_id=?'; args.append(str(user_id))
        query += ')'
        if flight_id is not None:
            query += ' AND f.id=?'; args.append(flight_id)
        else:
            # Coarse SQL bound, then authoritative UTC timestamp checks below.
            query += ' AND f.date >= ? AND f.date <= ?'
            args.extend([(now-timedelta(days=3)).date().isoformat(), (now+timedelta(days=1)).date().isoformat()])
        rows = db.execute(query, args).fetchall()
        result = [t for row in rows if (t := target(row))]
        if flight_id is not None:
            return bool(rows), result
        return [t for t in result if t['start'] <= now and t['end'] >= now-timedelta(hours=2)]
    finally:
        db.close()


def matches(message, flight):
    stamp = parse_time(message.get('receivedAt'))
    return bool(stamp and flight['start'] <= stamp <= flight['end']
                and tail(message.get('registration')) == tail(flight['registration'])
                and message.get('flight') and number(message['flight']) in flight['aliases'])
