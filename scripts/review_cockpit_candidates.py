"""Private operator inbox for aircraft messages requiring human review.

The cache holds only provider IDs for sensitive messages. Showing one requires
the root-only provider credential file; no source text is written to disk.
This tool never publishes a story.
"""
import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


IDENTIFIER = re.compile(r'[a-f0-9]{24}\Z')
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+')
URL = re.compile(r'https?://\S+|www\.\S+', re.I)
PHONE = re.compile(r'\+?\d[\d ()-]{8,}\d')


def redact(text):
    text = URL.sub('[URL]', EMAIL.sub('[EMAIL]', text))
    return PHONE.sub('[NUMBER]', text)


def read_key(path):
    for line in Path(path).read_text().splitlines():
        if line.startswith('AIRFRAMES_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise ValueError('Airframes key not available')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/var/lib/sofly-cockpit/stories.sqlite')
    parser.add_argument('--key-file', default='/etc/sofly/cockpit.env')
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--show', metavar='REVIEW_ID')
    parser.add_argument('--mark', metavar='REVIEW_ID')
    parser.add_argument('--decision', choices=('reviewed', 'dismissed'))
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or (args.show and args.mark) or bool(args.mark) != bool(args.decision):
        parser.error('Choose a limit from 1 to 100, or one valid action')
    identity = args.show or args.mark
    if identity and not IDENTIFIER.fullmatch(identity):
        parser.error('Invalid review ID')
    mode = 'rw' if args.mark else 'ro'
    db = sqlite3.connect(f'file:{Path(args.db).resolve()}?mode={mode}', uri=True)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec='seconds').replace('+00:00', 'Z')
        if args.mark:
            with db:
                changed = db.execute("UPDATE cockpit_review SET status=? WHERE id=? AND received>=? AND status='pending'",
                                     (args.decision, identity, cutoff)).rowcount
            print('Updated' if changed else 'No pending review found')
            return
        if args.show:
            row = db.execute('SELECT received,registration,provider_id,reason,status FROM cockpit_review WHERE id=? AND received>=?',
                             (identity, cutoff)).fetchone()
            if not row:
                raise SystemExit('Review record not found')
            key = read_key(args.key_file)
            response = httpx.get(f'https://api.airframes.io/v1/messages/{row[2]}',
                                 headers={'Authorization': 'Bearer ' + key}, timeout=15, follow_redirects=False)
            response.raise_for_status()
            message = response.json()
            if message.get('id') != row[2] or str(message.get('tail', '')).upper() != row[1]:
                raise SystemExit('Provider identity does not match review record')
            print(f'{identity} {row[0]} {row[1]} {row[3]} {row[4]}')
            print(redact(str(message.get('text', '')))[:2000])
            return
        for row in db.execute("SELECT id,received,registration,provider_id,reason FROM cockpit_review WHERE status='pending' AND received>=? ORDER BY received DESC LIMIT ?",
                              (cutoff, args.limit)):
            print(' '.join(map(str, row)))
    finally:
        db.close()


if __name__ == '__main__':
    main()
