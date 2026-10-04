"""Importance of an aircraft-message story, for the feed's Top view and alerts.

The kind of event decides the tier. Major is kept rare on purpose: something
serious happened (smoke, fire, an engine failure, a technical diversion), not
an everyday weather or fuel diversion. The editorial interest score orders
stories within a tier and keeps minor notes out of major. Classification reads
the published headline and excerpt on every read, so all stories follow the
current rules.
"""
import re
from datetime import datetime, timezone

TIERS = ('major', 'notable', 'background')
TIER_RANK = {'major': 2, 'notable': 1, 'background': 0}
# Major must also be a story the editors rated: a minor note stays notable.
MAJOR_MIN_INTEREST = 60
# Shared alerts: any major event, or only the stronger notable ones.
ALERT_FLOOR = {'major': MAJOR_MIN_INTEREST, 'notable': 65}
TOP_WINDOW_HOURS = 72
# Within a tier, a day of age costs as much as 10 points of editorial interest.
DECAY_POINTS_PER_DAY = 10

# Regulatory tarmac-delay uplinks say "...AIRBORNE OR RETURNING TO GATE": a
# delay notice, not a return. They are sent after every hour on the tarmac, so
# only a long one (two hours or more) is notable.
_TARMAC = re.compile(r'\bTARMAC DELAY\b|\bLTD TIMING\b')
_TARMAC_MINUTES = re.compile(r'\b(?:OFF GATE|TARMAC)\D{0,25}?(\d{2,3}) ?MIN')
LONG_TARMAC_MINUTES = 120
# Negated or conditional actions never count as the action.
_NEGATED = re.compile(r'\b(?:NOT|NO LONGER|IF|AVOID|UNABLE TO|NO NEED TO|NO PLANS? TO|CANCEL(?:LED|ING)?)\s+'
                      r'(?:\w+\s+)?(?:DIVERT\w*|RETURN\w*|GO[ -]?AROUND)')
# Returns on the ground (notable) and wording that is not a return at all.
_GROUND_RETURN = re.compile(r'\bRETURN(?:ING|ED|S)? TO (?:THE )?(?:GATE|STAND|RAMP|BLOCKS?)\b')
_NOT_A_RETURN = re.compile(r'\bRETURN(?:ING|ED|S)? TO (?:THE )?(?:SERVICE|NORMAL|SCHEDULE)\b')
# Serious on its own: smoke, fire or fumes, an engine failure or in-flight
# shutdown, a pressurization emergency, a rejected takeoff.
_SERIOUS = re.compile(r'\bSMOKE\b|\bFIRE\b|\bFUMES?\b|\bBURNING\b'
                      r'|\bENG(?:INE)?\s*\d?\s*(?:FAIL(?:URE|ED)?|FIRE|FLAME ?OUT)\b|\bIFSD\b|\bIN ?FLIGHT SHUT ?DOWN\b'
                      r'|\bDEPRESSURI[SZ](?:ATION|ED)\b|\bEMERGENCY DESCENT\b|\bREJECTED (?:TAKE ?OFF|T/O)\b')
# A diversion, return or strike is major only with a technical or safety cause;
# weather, fuel and closed-airport diversions are notable, everyday operations.
_ESCALATING_EVENT = re.compile(r'\bDIVERT(?:ING|ED|S)?\b|\bRETURN(?:ING|ED|S)? TO\b|\bAIR ?RETURN\b'
                               r'|\bBIRD ?STRIKE\b|\bLIGHTNING STRIKE\b|\bSTRUCK BY LIGHTNING\b')
# A system named with a problem ("ENG 2 FAILURE", "HYD LEAK"), or damage
# itself. A mention alone ("ENG CHECKED") is not a cause.
_TECHNICAL = re.compile(r'\b(?:ENG(?:INE)?S?|HYD(?:RAULIC)?S?|PRESSURI[SZ]ATION|GEAR|DOORS?|FLAPS?|BRAKES?'
                        r'|ELEC(?:TRICAL)?|GENERATORS?|WIND(?:SHIELD|SCREEN))\b(?:\s+\w+){0,3}?\s+'
                        r'(?:ISSUES?|PROBLEMS?|FAIL(?:URE|ED|S)?|INOP|MALFUNCTION|WARNING|FAULT|LOSS|LEAK|VIBRATIONS?|INDICATION)\b'
                        r'|\bCRACK(?:ED)?\b|\bDAMAGED?\b|\bMALFUNCTION\b|\bFAIL(?:URE|ED)\b|\bVIBRATIONS?\b'
                        r'|\bTECH(?:NICAL)? (?:ISSUES?|PROBLEMS?)\b')
# Reassurance, and possible or planned actions, never escalate.
_NO_ISSUE = re.compile(r'\bNO (?:DAMAGE|ISSUES?|PROBLEMS?|ABNORMALIT(?:Y|IES))\b|\bALL (?:OK|NORMAL)\b'
                       r'|\bNORMAL PARAMETERS\b|\bPARAMETROS NORMALES\b')
_TENTATIVE = re.compile(r'\b(?:MAY|MIGHT|COULD|POSSIBLE|POSSIBLY|PLAN(?:NING|S)?|CONSIDER(?:ING)?|OFF CHANCE)\b'
                        r'(?:\s+\w+){0,3}?\s+(?:DIVERT\w*|DIVERSION|RETURN\w*)')
_NOTABLE = re.compile(r'\bDIVERT(?:ING|ED|S)?\b|\bDIVERSION\b|\bRETURN(?:ING|ED|S)? TO\b|\bAIR ?RETURN\b|\bGO[ -]?AROUND\b'
                      r'|\bBIRD ?STRIKE\b|\bLIGHTNING STRIKE\b|\bSTRUCK BY LIGHTNING\b|\bVOLCANIC ASH\b'
                      r'|\bHOLDING\b|\bSPECIAL HANDLING\b|\bSMELL\b|\bODOU?R\b|\bIFE PANEL\b'
                      r'|\bBIRTHDAY\b|\bRETIREMENT\b|\bCONGRATULATIONS\b|\bCHRISTMAS\b|\bDEVIAT(?:ING|ED|ES|ION)\b'
                      r'|\bSEV(?:ERE)? TURB(?:ULENCE)?\b|\bGATE REQUEST\b')
def story_tier(title, transmission='', interest=None):
    text = ' '.join(f'{title or ""} {transmission or ""}'.upper().split())
    if not text:
        return 'background'
    if _TARMAC.search(text):
        minutes = [int(value) for value in _TARMAC_MINUTES.findall(text)]
        return 'notable' if minutes and max(minutes) >= LONG_TARMAC_MINUTES else 'background'
    text = _NEGATED.sub(' ', text)
    ground_return = bool(_GROUND_RETURN.search(text))
    text = _NOT_A_RETURN.sub(' ', _GROUND_RETURN.sub(' ', text))
    calm = _NO_ISSUE.sub(' ', text)
    escalated = (_ESCALATING_EVENT.search(calm) and not _TENTATIVE.search(calm) and _TECHNICAL.search(calm))
    if _SERIOUS.search(calm) or escalated:
        tier = 'major'
    elif ground_return or _NOTABLE.search(text):
        tier = 'notable'
    else:
        tier = 'background'
    if tier == 'major' and type(interest) is int and interest < MAJOR_MIN_INTEREST:
        tier = 'notable'
    return tier


def with_tier(story):
    """The tier under the current rules, from the published headline and excerpt;
    a stored tier from earlier rules is not trusted."""
    return dict(story, tier=story_tier(story.get('title'), story.get('transmission'), story.get('interestScore')))


def one_per_aircraft(stories):
    """Keep only each aircraft's first (most important) story: one event often
    arrives as several messages with different headlines."""
    seen, result = set(), []
    for story in stories:
        aircraft = str(story.get('registration') or '').upper().replace('-', '') or story.get('id')
        if aircraft not in seen:
            seen.add(aircraft)
            result.append(story)
    return result


def diversify(stories, run=1):
    """Keep importance order, but within a tier never show more than `run`
    stories of one category in a row while another category is waiting."""
    result, pending = [], list(stories)
    while pending:
        pick = 0
        recent = result[-run:]
        if len(recent) == run and len({story.get('category') for story in recent}) == 1:
            repeated = recent[-1].get('category')
            pick = next((index for index, story in enumerate(pending)
                         if story.get('tier') == pending[0].get('tier') and story.get('category') != repeated), 0)
        result.append(pending.pop(pick))
    return result


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
