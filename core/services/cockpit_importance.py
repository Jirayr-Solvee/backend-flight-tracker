"""Importance of an aircraft-message story, for the feed's Top view and alerts.

The kind of event decides the tier: something that changed this aircraft's
journey outranks routine operations, whatever the editorial score says. The
editorial interest score only orders stories within a tier. Classification
reads the published headline and excerpt, so stories stored before tiers
existed are ranked exactly like new ones.
"""
import re
from datetime import datetime, timezone

TIERS = ('major', 'notable', 'background')
TIER_RANK = {'major': 2, 'notable': 1, 'background': 0}
# Shared alerts: a major event with a real story, or only the strongest notable ones.
ALERT_FLOOR = {'major': 50, 'notable': 70}
TOP_WINDOW_HOURS = 72
# Within a tier, a day of age costs as much as 10 points of editorial interest.
DECAY_POINTS_PER_DAY = 10

# Regulatory tarmac-delay uplinks say "...AIRBORNE OR RETURNING TO GATE": a
# delay notice, not a return.
_TARMAC = re.compile(r'\bTARMAC DELAY\b|\bLTD TIMING\b')
# Negated or conditional actions never count as the action.
_NEGATED = re.compile(r'\b(?:NOT|NO LONGER|IF|AVOID|UNABLE TO|NO NEED TO|NO PLANS? TO|CANCEL(?:LED|ING)?)\s+'
                      r'(?:\w+\s+)?(?:DIVERT\w*|RETURN\w*|GO[ -]?AROUND)')
# Returns on the ground (notable) and wording that is not a return at all.
_GROUND_RETURN = re.compile(r'\bRETURN(?:ING|ED|S)? TO (?:THE )?(?:GATE|STAND|RAMP|BLOCKS?)\b')
_NOT_A_RETURN = re.compile(r'\bRETURN(?:ING|ED|S)? TO (?:THE )?(?:SERVICE|NORMAL|SCHEDULE)\b')
_MAJOR = re.compile(r'\bDIVERT(?:ING|ED|S)?\b|\bRETURN(?:ING|ED|S)? TO\b|\bAIR ?RETURN\b|\bGO[ -]?AROUND\b'
                    r'|\bREJECTED (?:TAKE ?OFF|T/O)\b|\bBIRD ?STRIKE\b|\bLIGHTNING STRIKE\b|\bSTRUCK BY LIGHTNING\b'
                    r'|\bSMOKE\b|\bFIRE\b|\bFUMES?\b|\bBURNING\b|\bVOLCANIC ASH\b')
_NOTABLE = re.compile(r'\bTARMAC DELAY\b|\bHOLDING\b|\bSPECIAL HANDLING\b|\bSMELL\b|\bODOU?R\b|\bIFE PANEL\b'
                      r'|\bBIRTHDAY\b|\bRETIREMENT\b|\bCONGRATULATIONS\b|\bCHRISTMAS\b|\bDEVIAT(?:ING|ED|ES|ION)\b'
                      r'|\bSEV(?:ERE)? TURB(?:ULENCE)?\b|\bGATE REQUEST\b')


def story_tier(title, transmission=''):
    text = ' '.join(f'{title or ""} {transmission or ""}'.upper().split())
    if not text:
        return 'background'
    if _TARMAC.search(text):
        return 'notable'
    text = _NEGATED.sub(' ', text)
    ground_return = bool(_GROUND_RETURN.search(text))
    text = _NOT_A_RETURN.sub(' ', _GROUND_RETURN.sub(' ', text))
    if _MAJOR.search(text):
        return 'major'
    if ground_return or _NOTABLE.search(text):
        return 'notable'
    return 'background'


def with_tier(story):
    """The stored tier, or one derived from the published text for older stories."""
    if story.get('tier') in TIER_RANK:
        return story
    return dict(story, tier=story_tier(story.get('title'), story.get('transmission')))


def importance_key(story, now=None):
    """Sort key: tier, then editorial interest (aged when `now` is given), then recency."""
    interest = story.get('interestScore')
    interest = interest if type(interest) is int else 0
    received = _parse(story.get('receivedAt'))
    if now is not None and received is not None:
        interest -= max(0.0, (now - received).total_seconds()) / 86400 * DECAY_POINTS_PER_DAY
    return (TIER_RANK.get(story.get('tier'), 0), round(interest),
            story.get('receivedAt') or '', story.get('id') or '')


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def alert_worthy(story):
    floor = ALERT_FLOOR.get(story.get('tier'))
    interest = story.get('interestScore')
    return floor is not None and type(interest) is int and interest >= floor
