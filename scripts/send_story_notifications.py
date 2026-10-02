"""Scheduled worker. Disabled unless exact supported release and enable flag are configured."""
import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def supported_releases():
    raw = os.environ.get('SOFLY_STORY_PUSH_RELEASES', '')
    if not raw:
        version = os.environ.get('SOFLY_STORY_PUSH_VERSION', '')
        build = os.environ.get('SOFLY_STORY_PUSH_BUILD', '0')
        raw = f'{version}:{build}' if version else ''
    result = set()
    for item in raw.split(','):
        if not item:continue
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2}:[1-9][0-9]{0,6}', item):
            raise ValueError('Invalid supported story-push release')
        version, build = item.split(':')
        result.add((version, int(build)))
    if not result:
        raise ValueError('No supported story-push releases configured')
    return result


async def main():
    if os.environ.get('SOFLY_STORY_PUSH_ENABLED') != '1':
        print(json.dumps({'enabled': False})); return
    environment = os.environ.get('SOFLY_STORY_PUSH_ENVIRONMENT', 'production')
    qa_device_id = os.environ.get('SOFLY_STORY_PUSH_QA_DEVICE_ID')
    if environment == 'testflight':
        if os.environ.get('SOFLY_STORY_PUSH_QA_ENABLED') != '1' or not qa_device_id:
            raise ValueError('TestFlight story push requires exact QA opt-in and device')
        qa_device_id = str(uuid.UUID(qa_device_id))
    elif environment != 'production' or qa_device_id or os.environ.get('SOFLY_STORY_PUSH_QA_ENABLED') == '1':
        raise ValueError('Invalid story-push environment or QA scope')
    from core.models import engine  # Ensure new tables exist before opening raw transactions.
    from core.services.story_push import dispatch
    from core.services.apn.service import get_apns_client
    from aioapns import NotificationRequest, PushType

    async def send(token, payload, identifier):
        request = NotificationRequest(device_token=token, message=payload,
                                      notification_id=identifier, time_to_live=3600, push_type=PushType.ALERT)
        response = await asyncio.wait_for(get_apns_client().send_notification(request), timeout=20)
        return response.status

    metrics = await dispatch(
        os.environ.get('SOFLY_FLIGHT_DB', 'database.db'), os.environ['SOFLY_COCKPIT_DB'], send,
        enabled=True, version=os.environ.get('SOFLY_STORY_PUSH_VERSION', ''),
        build=int(os.environ.get('SOFLY_STORY_PUSH_BUILD', '0')),
        environment=environment, releases=supported_releases(),
        qa_device_id=qa_device_id, max_sends=20 if qa_device_id else 200,
    )
    print(json.dumps(metrics))


if __name__ == '__main__':
    try: asyncio.run(main())
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__})); sys.exit(1)
