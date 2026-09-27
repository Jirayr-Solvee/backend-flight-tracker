"""Run explicitly as root on the backend host; Airframes key via stdin only."""
import os
import re
import sys
from pathlib import Path
from dotenv import dotenv_values

if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Root required')
    air=sys.stdin.read().strip()
    gem=dotenv_values('/home/ubuntu/backend-flight-tracker/.env').get('GEMINI_API_KEY','')
    if not all(re.fullmatch(r'[A-Za-z0-9_\-\.]+',value or '') for value in (air,gem)):
        raise SystemExit('Missing or invalid credential format')
    directory=Path('/etc/sofly')
    directory.mkdir(mode=0o700,exist_ok=True)
    target=directory/'cockpit.env'
    temporary=directory/'cockpit.env.new'
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write('AIRFRAMES_API_KEY='+air+'\nGEMINI_API_KEY='+gem+'\n')
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,target)
    print('Cockpit credentials configured; values not displayed.')
