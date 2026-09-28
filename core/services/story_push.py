"""Bounded, at-most-once story notifications. No AI work and no notification text logs."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .cockpit_stories import parse_time, utc_string


def slot_for(now, time_zone):
    local = now.astimezone(ZoneInfo(time_zone))
    # One two-hour delivery window per slot; no nighttime catch-up bursts.
    slot = next((i for i, hour in enumerate((10, 15, 19)) if hour <= local.hour < hour + 2), None)
    return local.date().isoformat(), slot


def eligible(device, now, version, build, environment='production'):
    return (bool(version) and build > 0 and device['app_version'] == version
            and device['build_number'] == build and device['capability'] == 1
            and device['enabled'] == 1 and device['apn_token_active'] == 1
            and bool(device['apn_token']) and device['environment'] == environment
            and int(now.timestamp()) - 30 * 86400 <= device['updated_at'] <= int(now.timestamp()) + 60
            and device['user_id'] == device['owner_id'])


def candidates(path, now):
    # Read-only: missing/stale/corrupt feed must never create an empty new store.
    with closing(sqlite3.connect('file:' + path + '?mode=ro', uri=True)) as db:
        row = db.execute("SELECT value FROM cockpit_metadata WHERE key='updated_at'").fetchone()
        if not row or not parse_time(row[0]) or now - parse_time(row[0]) > timedelta(hours=2):
            return []
        result = []
        for (raw,) in db.execute('SELECT payload FROM cockpit_stories WHERE received>=? ORDER BY received DESC LIMIT 500',
                                (utc_string(now-timedelta(hours=24)),)):
            story = json.loads(raw)
            received = parse_time(story.get('receivedAt'))
            if (story.get('notificationEligible') is True and type(story.get('interestScore')) is int
                    and story['interestScore'] >= 80 and received and received <= now
                    and now-received <= timedelta(hours=24)):
                result.append(story)
        return sorted(result, key=lambda x: x['interestScore'], reverse=True)


def fingerprint(story):
    # Avoid repeated transmissions even if a different record/date produced a new id.
    return hashlib.sha256(' '.join(story['transmission'].upper().split()).encode()).hexdigest()


def reserve(db, device, stories, now):
    day, slot = slot_for(now, device['time_zone'])
    if slot is None: return None
    stamp = int(now.timestamp())
    db.execute('BEGIN IMMEDIATE')
    try:
        history = db.execute('SELECT * FROM storypushdelivery WHERE user_id=? AND reserved_at>?',
                             (device['user_id'], stamp-90*86400)).fetchall()
        recent = [r for r in history if r['reserved_at'] > stamp-86400]
        if (len(recent) >= 3 or any(stamp-r['reserved_at'] < 3*3600 for r in recent)
                or any(r['local_day'] == day and r['slot'] == slot for r in recent)):
            db.rollback(); return None
        seen = {r['story_fingerprint'] for r in history}
        story = next((s for s in stories if fingerprint(s) not in seen), None)
        if story is None: db.rollback(); return None
        delivery = str(uuid.uuid4())
        db.execute('INSERT INTO storypushdelivery (id,user_id,device_id,story_id,story_fingerprint,reserved_at,local_day,slot,status) VALUES (?,?,?,?,?,?,?,?,?)',
                   (delivery, device['user_id'], device['device_id'], story['id'], fingerprint(story), stamp, day, slot, 'reserved'))
        db.commit()
        return delivery, story
    except Exception:
        db.rollback(); raise


def payload(story, language, delivery):
    copy = story if language == 'en' else story.get('translations', {}).get(language)
    if not copy or not copy.get('title') or not copy.get('summary'): return None
    value = {'aps': {'alert': {'title': copy['title'], 'body': copy['summary']},
                     'sound': 'default', 'thread-id': 'aircraft-stories'},
             'notification_type': 'aircraft_story', 'notification_id': delivery, 'story_id': story['id']}
    return value if len(json.dumps(value, ensure_ascii=False).encode()) <= 4096 else None


async def dispatch(database, feed, send, *, enabled=False, version='', build=0,
                   environment='production', now=None, max_sends=200):
    metrics = dict(eligible=0, reserved=0, accepted=0, failed=0)
    if not enabled: return metrics
    now = now or datetime.now(timezone.utc)
    stories = candidates(feed, now)
    if not stories: return metrics
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        devices = db.execute('SELECT p.*, d.user_id AS owner_id,d.apn_token,d.apn_token_active FROM storypushdevice p JOIN device d ON p.device_id=d.id ORDER BY p.updated_at DESC').fetchall()
        for device in devices:
            if metrics['reserved'] >= max_sends: break
            if not eligible(device, now, version, build, environment): continue
            metrics['eligible'] += 1
            suitable = [s for s in stories if payload(s, device['language'], str(uuid.UUID(int=0))) is not None]
            result = reserve(db, device, suitable, now)
            if not result: continue
            delivery, story = result
            metrics['reserved'] += 1
            try:
                # Recheck ownership, preference, version and token after reservation.
                current = db.execute('SELECT p.*,d.user_id AS owner_id,d.apn_token,d.apn_token_active FROM storypushdevice p JOIN device d ON p.device_id=d.id WHERE p.device_id=?', (device['device_id'],)).fetchone()
                if not current or not eligible(current, now, version, build, environment) or current['user_id'] != device['user_id'] or current['apn_token'] != device['apn_token']:
                    outcome = 'suppressed'
                else:
                    status = await send(device['apn_token'], payload(story, device['language'], delivery), delivery)
                    outcome = 'accepted' if str(status) == '200' else 'failed'
                metrics['accepted' if outcome == 'accepted' else 'failed'] += 1
            except Exception:
                outcome = 'unknown'; metrics['failed'] += 1
            db.execute('UPDATE storypushdelivery SET status=? WHERE id=?', (outcome, delivery))
            db.commit()
        db.execute('DELETE FROM storypushdelivery WHERE reserved_at<?', (int(now.timestamp())-90*86400,))
        db.commit()
    return metrics
