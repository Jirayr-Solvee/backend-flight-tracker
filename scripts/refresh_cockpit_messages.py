"""Scheduled refresh. No interactive secrets or overlapping workers."""
import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from core.services.cockpit_ingestion import run
from core.services.cockpit_tracking import load_targets


async def refresh():
    path = os.environ.get('SOFLY_COCKPIT_DB')
    if not path:
        raise SystemExit('Set SOFLY_COCKPIT_DB to a dedicated cache database path.')
    air, gem = os.environ['AIRFRAMES_API_KEY'], os.environ['GEMINI_API_KEY']
    if not air or not gem: raise ValueError('credentials')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path+'.lock','a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
            targets=load_targets(os.environ.get('SOFLY_FLIGHT_DB','database.db'))
            metrics=await run(path,air,gem,client,targets=targets)
            print(json.dumps(metrics))
            if metrics['errors']: raise SystemExit(1)


if __name__ == '__main__':
    try: asyncio.run(refresh())
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}))
        sys.exit(1)
