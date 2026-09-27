"""Authenticated read-only message feed. Provider access is an offline job."""
import os
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from ..dependency import get_current_user
from ..services.cockpit_stories import read_stories

router = APIRouter()


@router.get('/stories')
def stories(registration: str | None = Query(None, min_length=3, max_length=12, pattern=r'^[A-Za-z0-9-]+$'),
            user=Depends(get_current_user)):
    path = os.environ.get('SOFLY_COCKPIT_DB')
    if not path:
        raise HTTPException(503, 'Aircraft messages are not available yet')
    try:
        result = read_stories(path, registration)
    except (sqlite3.Error, ValueError, OSError):
        raise HTTPException(503, 'Aircraft messages are temporarily unavailable') from None
    if result is None:
        raise HTTPException(503, 'Aircraft messages are temporarily unavailable')
    return result
