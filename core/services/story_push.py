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
from .cockpit_ingestion import routine_message
from .cockpit_tracking import load_targets, matches

MIN_SHARED_SCORE = 65
SHARED_STORY_MAX_AGE = timedelta(hours=72)
FLIGHT_STORY_MAX_AGE = timedelta(hours=2)


def slot_for(now, time_zone):
    local = now.astimezone(ZoneInfo(time_zone))
    # One two-hour delivery window per slot; no nighttime catch-up bursts.
    slot = next((i for i, hour in enumerate((10, 15, 19)) if hour <= local.hour < hour + 2), None)
    return local.date().isoformat(), slot


def eligible(device, now, version, build, environment='production', releases=None,
             qa_device_id=None):
    supported = releases if releases is not None else {(version, build)}
    qa_entitlement = (environment == 'testflight' and qa_device_id is not None
                      and device['device_id'] == qa_device_id)
    return (bool(supported) and (device['app_version'], device['build_number']) in supported
            and device['capability'] == 1
            and device['enabled'] == 1 and device['apn_token_active'] == 1
            and bool(device['apn_token']) and device['environment'] == environment
            and int(now.timestamp()) - 30 * 86400 <= device['updated_at'] <= int(now.timestamp()) + 60
            and device['user_id'] == device['owner_id']
            and (qa_entitlement or (type(device['premium_valid_until']) is int
                                    and device['premium_valid_until'] > int(now.timestamp() * 1000))))


def candidates(path, now):
    # Read-only: missing/stale/corrupt feed must never create an empty new store.
    with closing(sqlite3.connect('file:' + path + '?mode=ro', uri=True)) as db:
        row = db.execute("SELECT value FROM cockpit_metadata WHERE key='updated_at'").fetchone()
        if not row or not parse_time(row[0]) or now - parse_time(row[0]) > timedelta(hours=2):
            return []
        result = []
        for (raw,) in db.execute('SELECT payload FROM cockpit_stories WHERE received>=? ORDER BY received DESC LIMIT 1000',
                                (utc_string(now-timedelta(days=7)),)):
            story = json.loads(raw)
            received = parse_time(story.get('receivedAt'))
            # Older stories stored a false notificationEligible solely because
            # the previous editorial score floor was 80; use the reviewed score.
            if (type(story.get('interestScore')) is int and story['interestScore'] >= 30
                    and isinstance(story.get('transmission'), str)
                    and not routine_message(story['transmission'])
                    and received and received <= now):
                result.append(story)
        return sorted(result, key=lambda x: (x['interestScore'], x['receivedAt'], x['id']), reverse=True)


def fingerprint(story):
    # Avoid repeated transmissions even if a different record/date produced a new id.
    return hashlib.sha256(' '.join(story['transmission'].upper().split()).encode()).hexdigest()


def campaign_for(db, stories, day, slot, now):
    """Freeze one shared story before any recipient is sent a notification."""
    db.execute('BEGIN IMMEDIATE')
    try:
        existing = db.execute('SELECT story_id FROM storypushcampaign WHERE day=? AND slot=?', (day, slot)).fetchone()
        if existing:
            db.commit()
            return next((story for story in stories if story['id'] == existing['story_id']), None)
        cutoff = int(now.timestamp()) - 90*86400
        used = {row[0] for row in db.execute('SELECT story_fingerprint FROM storypushcampaign WHERE created_at>?', (cutoff,))}
        today_categories = {row[0] for row in db.execute('SELECT category FROM storypushcampaign WHERE day=?', (day,))}
        available = [s for s in stories if s['interestScore'] >= MIN_SHARED_SCORE
                     and now-parse_time(s['receivedAt']) <= SHARED_STORY_MAX_AGE
                     and fingerprint(s) not in used]
        # Prefer an aircraft event and a category not already sent today.
        rank = {'Diversion': 5, 'Cabin': 4, 'Operations': 3, 'Crew': 2, 'Weather': 1, 'Cargo': 0}
        available.sort(key=lambda s: (s.get('category') not in today_categories,
                                      rank.get(s.get('category'), 0), s['interestScore'], s['receivedAt']), reverse=True)
        story = available[0] if available else None
        if story:
            db.execute('INSERT INTO storypushcampaign VALUES (?,?,?,?,?,?)',
                       (day, slot, story['id'], fingerprint(story), story.get('category', ''), int(now.timestamp())))
        db.commit()
        return story
    except Exception:
        db.rollback(); raise


def reserve(db, device, story, now, *, flight=False):
    day, slot = slot_for(now, device['time_zone'])
    if slot is None and not flight: return None
    stamp = int(now.timestamp())
    identity = fingerprint(story)
    db.execute('BEGIN IMMEDIATE')
    try:
        history = db.execute('SELECT * FROM storypushdelivery WHERE user_id=? AND reserved_at>?',
                             (device['user_id'], stamp-90*86400)).fetchall()
        if any(r['device_id'] == device['device_id'] and r['story_fingerprint'] == identity for r in history):
            db.rollback(); return None
        if flight:
            # A shared alert already told this account about the same message.
            if any(r['story_fingerprint'] == identity and r['slot'] >= 0 for r in history):
                db.rollback(); return None
        else:
            recent = [r for r in history if r['reserved_at'] > stamp-86400 and r['slot'] >= 0]
            slots = {(r['local_day'], r['slot']) for r in recent}
            same_slot = [r for r in recent if r['local_day'] == day and r['slot'] == slot]
            if (len(slots) >= 3 and (day, slot) not in slots
                    or any(stamp-r['reserved_at'] < 3*3600 for r in recent if (r['local_day'],r['slot']) != (day,slot))
                    or any(r['story_id'] != story['id'] for r in same_slot)):
                db.rollback(); return None
        delivery = str(uuid.uuid4())
        db.execute('INSERT INTO storypushdelivery (id,user_id,device_id,story_id,story_fingerprint,reserved_at,local_day,slot,status) VALUES (?,?,?,?,?,?,?,?,?)',
                   (delivery, device['user_id'], device['device_id'], story['id'], identity, stamp, day, -1 if flight else slot, 'reserved'))
        db.commit()
        return delivery
    except Exception:
        db.rollback(); raise


def payload(story, language, delivery, *, flight=False):
    copy = story if language == 'en' else story.get('translations', {}).get(language)
    if not copy or not copy.get('title') or not copy.get('summary'): return None
    alert = {'title': copy['title'], 'body': copy['summary']}
    if flight and story.get('flight'): alert['subtitle'] = story['flight']
    value = {'aps': {'alert': alert,
                     'sound': 'default', 'thread-id': 'flight-messages' if flight else 'aircraft-stories'},
             'notification_type': 'aircraft_story', 'notification_id': delivery, 'story_id': story['id']}
    return value if len(json.dumps(value, ensure_ascii=False).encode()) <= 4096 else None


async def dispatch(database, feed, send, *, enabled=False, version='', build=0,
                   environment='production', releases=None, now=None, max_sends=200,
                   qa_device_id=None):
    metrics = dict(eligible=0, reserved=0, accepted=0, failed=0, flight_reserved=0)
    if not enabled: return metrics
    if environment not in ('production', 'testflight'):
        raise ValueError('Unsupported story-push environment')
    if environment == 'testflight' and not qa_device_id:
        raise ValueError('TestFlight delivery requires one exact device')
    if environment == 'production' and qa_device_id:
        raise ValueError('QA device cannot run in production delivery')
    now = now or datetime.now(timezone.utc)
    stories = candidates(feed, now)
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        query = 'SELECT p.*, d.user_id AS owner_id,d.apn_token,d.apn_token_active,u.premium_valid_until FROM storypushdevice p JOIN device d ON p.device_id=d.id JOIN user u ON u.id=p.user_id'
        devices = (db.execute(query + ' WHERE p.device_id=?', (qa_device_id,)).fetchall()
                   if qa_device_id else db.execute(query + ' ORDER BY p.updated_at DESC').fetchall())
        campaign_cache = {}
        target_cache = {}
        for device in devices:
            if metrics['reserved'] >= max_sends: break
            if not eligible(device, now, version, build, environment, releases,
                            qa_device_id=qa_device_id): continue
            metrics['eligible'] += 1
            day,slot = slot_for(now, device['time_zone'])
            sends=[]
            if slot is not None and stories:
                campaign_key=(day,slot)
                if campaign_key not in campaign_cache:
                    campaign_cache[campaign_key]=campaign_for(db, stories, day, slot, now)
                story=campaign_cache[campaign_key]
                if story and payload(story,device['language'],str(uuid.UUID(int=0))):
                    sends.append((story,False))
            for story in stories:
                received=parse_time(story['receivedAt'])
                if not received or now-received>FLIGHT_STORY_MAX_AGE or not story.get('flight'):continue
                if not payload(story,device['language'],str(uuid.UUID(int=0)),flight=True):continue
                key=(device['user_id'],story['id'])
                if key not in target_cache:
                    targets=load_targets(database,user_id=device['user_id'],now=received)
                    target_cache[key]=any(matches(story,target) for target in targets)
                if target_cache[key]:sends.append((story,True))
            for story,is_flight in sends:
                if metrics['reserved'] >= max_sends:break
                delivery=reserve(db,device,story,now,flight=is_flight)
                if not delivery:continue
                metrics['reserved'] += 1
                if is_flight:metrics['flight_reserved'] += 1
                try:
                    # Recheck ownership, entitlement/QA scope, preference and token after reservation.
                    current = db.execute('SELECT p.*,d.user_id AS owner_id,d.apn_token,d.apn_token_active,u.premium_valid_until FROM storypushdevice p JOIN device d ON p.device_id=d.id JOIN user u ON u.id=p.user_id WHERE p.device_id=?', (device['device_id'],)).fetchone()
                    if not current or not eligible(current, now, version, build, environment, releases,
                                                   qa_device_id=qa_device_id) or current['user_id'] != device['user_id'] or current['apn_token'] != device['apn_token']:
                        outcome = 'suppressed'
                    else:
                        status = await send(device['apn_token'], payload(story, device['language'], delivery, flight=is_flight), delivery)
                        outcome = 'accepted' if str(status) == '200' else 'failed'
                    metrics['accepted' if outcome == 'accepted' else 'failed'] += 1
                except Exception:
                    outcome = 'unknown'; metrics['failed'] += 1
                db.execute('UPDATE storypushdelivery SET status=? WHERE id=?', (outcome, delivery))
                db.commit()
        db.execute('DELETE FROM storypushdelivery WHERE reserved_at<?', (int(now.timestamp())-90*86400,))
        db.execute('DELETE FROM storypushcampaign WHERE created_at<?', (int(now.timestamp())-90*86400,))
        db.commit()
    return metrics
