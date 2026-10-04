"""Authenticated read-only message feed. Provider access is an offline job."""
import os
import sqlite3
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from ..dependency import get_current_user
from ..services.cockpit_stories import read_stories, decode_cursor, utc_string
from ..services.cockpit_importance import with_tier
from ..services.cockpit_tracking import load_targets

router = APIRouter()
Category = Literal['Weather','Diversion','Crew','Cargo','Cabin','Operations']


@router.get('/stories/{story_id}')
def story_detail(story_id: str, user=Depends(get_current_user)):
    import re
    if not re.fullmatch(r'[a-f0-9]{24}', story_id):
        raise HTTPException(404, 'Message not found')
    path = os.environ.get('SOFLY_COCKPIT_DB')
    if not path: raise HTTPException(503, 'Aircraft messages are temporarily unavailable')
    try:
        with closing(sqlite3.connect('file:' + path + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT payload FROM cockpit_stories WHERE id=? AND received>=?',
                             (story_id, utc_string(datetime.now(timezone.utc)-timedelta(days=7)))).fetchone()
        if not row: raise HTTPException(404, 'Message no longer available')
        return with_tier(json.loads(row[0]))
    except (sqlite3.Error, ValueError, OSError):
        raise HTTPException(503, 'Aircraft messages are temporarily unavailable') from None


def check_cursor(cursor):
    try:decode_cursor(cursor)
    except ValueError:raise HTTPException(422,'Invalid message cursor') from None


@router.get('/stories')
def stories(registration: str | None = Query(None, min_length=3, max_length=12, pattern=r'^[A-Za-z0-9-]+$'),
            limit: int = Query(20,ge=1,le=50), category: Category | None = None,
            cursor: str | None = Query(None,max_length=512),
            sort: Literal['latest','top'] = 'latest',
            user=Depends(get_current_user)):
    # Older app versions never send sort and keep the chronological, paged feed.
    check_cursor(cursor)
    path = os.environ.get('SOFLY_COCKPIT_DB')
    if not path:
        raise HTTPException(503, 'Aircraft messages are not available yet')
    try:
        result = read_stories(path, registration,limit=limit,category=category,cursor=cursor,sort=sort)
    except (sqlite3.Error, ValueError, OSError):
        raise HTTPException(503, 'Aircraft messages are temporarily unavailable') from None
    if result is None:
        raise HTTPException(503, 'Aircraft messages are temporarily unavailable')
    return result


@router.get('/flights/{flight_id}/stories')
def flight_stories(flight_id: int, limit: int = Query(20,ge=1,le=50),
                   category: Category | None = None, cursor: str | None = Query(None,max_length=512),
                   user=Depends(get_current_user)):
    check_cursor(cursor)
    try:
        owned, targets = load_targets(os.environ.get('SOFLY_FLIGHT_DB','database.db'),
                                     user_id=user.id, flight_id=flight_id)
        if not owned:
            raise HTTPException(404, 'Tracked flight not found')
        if not targets:
            return {'stories':[], 'updatedAt':None, 'nextCursor':None, 'coverage':'awaiting_aircraft_or_schedule'}
        path=os.environ.get('SOFLY_COCKPIT_DB')
        result=read_stories(path,flight=targets[0],limit=limit,category=category,cursor=cursor) if path else None
        if result is None:
            raise HTTPException(503, 'Aircraft messages are temporarily unavailable')
        result['coverage']='matched_flight_window'
        return result
    except (sqlite3.Error, ValueError, OSError):
        raise HTTPException(503, 'Aircraft messages are temporarily unavailable') from None
