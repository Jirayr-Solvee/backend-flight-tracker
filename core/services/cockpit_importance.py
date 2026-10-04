"""Importance of aircraft-message stories, for the feed's Top view and alerts.

Each story gets an event kind (what happened, as a reader sees it) and an
importance score: the editorial interest adjusted by kind, so a bird strike
outranks an equally rated ground-power request. Tiers:

- major: each UTC day's highlights (at most two, of different kinds), plus
  any serious event such as smoke, fire or an engine failure;
- notable: worth a look (score of 60 or more);
- background: everything else.

The refresh job ranks the whole cache and stores kind, score and tier on each
story, so reads and alerts agree. A story read before its first ranking gets
its kind, score and tier without the daily highlight.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

TIER_RANK = {'major': 2, 'notable': 1, 'background': 0}
TOP_WINDOW_HOURS = 72
# Within a tier, a day of age costs 10 points of importance.
DECAY_POINTS_PER_DAY = 10
NOTABLE_SCORE = 60
HIGHLIGHT_SCORE = 60
HIGHLIGHTS_PER_DAY = 2
SERIOUS_MIN_INTEREST = 60
# Alerts: every major story, and only strong notable ones.
NOTABLE_ALERT_SCORE = 70

# Reader-facing kinds and their weight against editorial interest: rarer,
# human or physical events up; everyday route changes slightly down; routine
# ground operations well down (only a long tarmac delay is notable).
KIND_WEIGHT = {'safety': 25, 'strike': 10, 'crew': 10, 'cabin': 5, 'cargo': 5,
               'weather': 0, 'other': 0, 'route': -5, 'ground': -20}
# Ground operations are never a day's highlight.
_NO_HIGHLIGHT_KINDS = {'ground'}

# Regulatory tarmac-delay uplinks say "...AIRBORNE OR RETURNING TO GATE": a
# delay notice, not a return, sent after every hour; long ones still matter.
_TARMAC = re.compile(r'\bTARMAC DELAY\b|\bLTD TIMING\b')
_TARMAC_MINUTES = re.compile(r'\b(?:OFF GATE|TARMAC)\D{0,25}?(\d{2,3}) ?MIN')
LONG_TARMAC_MINUTES = 120
# Negated or conditional actions never count as the action.
_NEGATED = re.compile(r'\b(?:NOT|NO LONGER|IF|AVOID|UNABLE TO|NO NEED TO|NO PLANS? TO|CANCEL(?:LED|ING)?)\s+'
                      r'(?:\w+\s+)?(?:DIVERT\w*|RETURN\w*|GO[ -]?AROUND)')
_NOT_A_RETURN = re.compile(r'\bRETURN(?:ING|ED|S)? TO (?:THE )?(?:SERVICE|NORMAL|SCHEDULE)\b')
# Serious on its own: smoke, fire or fumes, an engine failure or in-flight
# shutdown, a pressurization emergency, a rejected takeoff.
_SERIOUS = re.compile(r'\bSMOKE\b|\bFIRE\b|\bFUMES?\b|\bBURNING\b'
                      r'|\bENG(?:INE)?\s*\d?\s*(?:FAIL(?:URE|ED)?|FIRE|FLAME ?OUT)\b|\bIFSD\b|\bIN ?FLIGHT SHUT ?DOWN\b'
                      r'|\bDEPRESSURI[SZ](?:ATION|ED)\b|\bEMERGENCY DESCENT\b|\bREJECTED (?:TAKE ?OFF|T/O)\b')
# A diversion, return or strike with a technical cause is serious too.
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
# Weather outlooks are not something that happened to this aircraft.
_OUTLOOK = re.compile(r'\b(?:FORECAST|PREDICTED|EXPECTED|POSSIBLE|POSSIBLY|WARNING|CAUTION|ADVISORY|OUTLOOK)\b')
# First match wins: a diversion around storms is a route change, not weather.
_KINDS = (
    ('strike', re.compile(r'\bBIRD ?STRIKE\b|\bLIGHTNING\b|\bSTRUCK BY\b|\bLASER\b')),
    ('crew', re.compile(r'\bBIRTHDAY\b|\bRETIRE(?:MENT|S|D|ING)?\b|\bCONGRAT\w*|\bCHRISTMAS\b|\bFIRST FLIGHT\b'
                        r'|\bLAST FLIGHT\b|\bPROPOS\w*|\bWEDDING\b|\bGATE REQUEST\b')),
    ('cabin', re.compile(r'\bSMELL\w*|\bODOU?RS?\b|\bIFE\b|\bGALLEY\b|\bOVEN\b|\bCATERING\b|\bCOFFEE\b'
                         r'|\bCABIN TEMP\w*|\bLAV(?:ATORY|S)?\b')),
    ('cargo', re.compile(r'\bSPECIAL HANDLING\b|\bLIVE ANIMALS?\b|\bANIMALS?\b|\bHORSES?\b|\bDOGS?\b|\bPETS?\b')),
    ('route', re.compile(r'\bDIVERT\w*|\bDIVERSION\b|\bRETURN(?:ING|ED|S)? TO\b|\bAIR ?RETURN\b|\bGO[ -]?AROUND\b'
                         r'|\bMISSED APPROACH\b|\bHOLDING\b|\bDEVIAT\w*|\bREROUTE\w*')),
    ('weather', re.compile(r'\bTURB\w*|\bVOLCANIC\b|\bCONVECTIVE\b|\bTHUNDERSTORMS?\b|\bTS\b|\bHAIL\b|\bICING\b'
                           r'|\bWIND ?SHEAR\b|\bWEATHER\b|\bWX\b')),
    ('ground', re.compile(r'\bTARMAC\b|\bAPU\b|\bGPU\b|\bGROUND POWER\b|\bMAINT\w*|\bMX\b|\bINOP\w*')),
)


def _text(story):
    return ' '.join(f'{story.get("title") or ""} {story.get("transmission") or ""}'.upper().split())


def classify(story):
    """(kind, importance score, serious) from the published headline and excerpt."""
    interest = story.get('interestScore')
    interest = interest if type(interest) is int else 40
    text = _text(story)
    if not text:
        return 'other', interest - 20, False
    tarmac = bool(_TARMAC.search(text))
    long_tarmac = tarmac and any(int(m) >= LONG_TARMAC_MINUTES for m in _TARMAC_MINUTES.findall(text))
    text = _NOT_A_RETURN.sub(' ', _NEGATED.sub(' ', text))
    calm = _NO_ISSUE.sub(' ', text)
    tentative = bool(_TENTATIVE.search(calm))
    serious = bool(_SERIOUS.search(calm)
                   or (_ESCALATING_EVENT.search(calm) and not tentative and _TECHNICAL.search(calm)))
    if serious:
        kind = 'safety'
    elif tarmac:
        kind = 'ground'
    else:
        kind = next((name for name, pattern in _KINDS if pattern.search(text)), 'other')
    score = interest + KIND_WEIGHT[kind]
    if kind == 'route' and tentative:
        score -= 10
    if kind == 'weather' and _OUTLOOK.search(text):
        score -= 15
    if long_tarmac:
        score += 15
    return kind, score, serious and interest >= SERIOUS_MIN_INTEREST


def _base_tier(score, serious):
    if serious:
        return 'major'
    return 'notable' if score >= NOTABLE_SCORE else 'background'


def rank_stories(stories):
    """id -> {kind, importance, tier} for a whole cache, with daily highlights:
    each UTC day's best notable stories, at most two and of different kinds."""
    ranked, by_day = {}, defaultdict(list)
    for story in stories:
        kind, score, serious = classify(story)
        ranked[story['id']] = dict(kind=kind, importance=score, tier=_base_tier(score, serious))
        if not serious and score >= HIGHLIGHT_SCORE and kind not in _NO_HIGHLIGHT_KINDS:
            by_day[str(story.get('receivedAt') or '')[:10]].append((score, story.get('receivedAt') or '', story['id'], kind))
    for candidates in by_day.values():
        kinds = set()
        for score, _, key, kind in sorted(candidates, reverse=True):
            if len(kinds) >= HIGHLIGHTS_PER_DAY:
                break
            if kind not in kinds:
                kinds.add(kind)
                ranked[key]['tier'] = 'major'
    return ranked


def assign_importance(db):
    """Rank every cached story and store kind, importance and tier on it.
    Returns how many stories changed."""
    rows = db.execute('SELECT id, payload FROM cockpit_stories').fetchall()
    stories = [dict(json.loads(payload), id=key) for key, payload in rows]
    ranked = rank_stories(stories)
    updates = []
    for story in stories:
        updated = dict(story, **ranked[story['id']])
        if updated != story:
            updates.append((json.dumps(updated), story['id']))
    with db:
        db.executemany('UPDATE cockpit_stories SET payload=? WHERE id=?', updates)
    return len(updates)


def with_tier(story):
    """The stored ranking, or kind, score and tier computed now (without the
    daily highlight) for a story the refresh job has not ranked yet."""
    if (story.get('tier') in TIER_RANK and story.get('kind') in KIND_WEIGHT
            and type(story.get('importance')) is int):
        return story
    kind, score, serious = classify(story)
    return dict(story, kind=kind, importance=score, tier=_base_tier(score, serious))


def importance_key(story, now=None):
    """Sort key: tier, then importance (aged when `now` is given), then recency."""
    score = story.get('importance')
    if type(score) is not int:
        score = story.get('interestScore') if type(story.get('interestScore')) is int else 0
    received = _parse(story.get('receivedAt'))
    if now is not None and received is not None:
        score -= max(0.0, (now - received).total_seconds()) / 86400 * DECAY_POINTS_PER_DAY
    return (TIER_RANK.get(story.get('tier'), 0), round(score),
            story.get('receivedAt') or '', story.get('id') or '')


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
    stories of one kind in a row while another kind is waiting."""
    result, pending = [], list(stories)
    while pending:
        pick = 0
        recent = result[-run:]
        if len(recent) == run and len({story.get('kind') for story in recent}) == 1:
            repeated = recent[-1].get('kind')
            pick = next((index for index, story in enumerate(pending)
                         if story.get('tier') == pending[0].get('tier') and story.get('kind') != repeated), 0)
        result.append(pending.pop(pick))
    return result


def alert_worthy(story):
    score = story.get('importance')
    return story.get('tier') == 'major' or (story.get('tier') == 'notable' and type(score) is int
                                            and score >= NOTABLE_ALERT_SCORE)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None
