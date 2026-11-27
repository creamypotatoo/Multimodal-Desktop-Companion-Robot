"""Deterministic, allowlisted voice intent parsing."""
import re

SMALL = dict(zip(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split(),
    range(20)))
TENS = dict(zip('twenty thirty forty fifty sixty seventy eighty ninety'.split(), range(20, 100, 10)))
UNITS = {'second': 1, 'minute': 60, 'hour': 3600}


def normalize(text):
    return ' '.join(re.findall(r"[a-z]+|[0-9]+", text.lower()))


def matches_wake(text, phrase):
    def tokens(value):
        return tuple('eli' if word in ('eli', 'ellie', 'ely') else word
                     for word in normalize(value).split())

    return tokens(text) == tokens(phrase)


def number(text):
    if text.isdecimal():
        return int(text) if len(text) <= 4 else None
    if text in ('a', 'an'):
        return 1
    words = text.split()
    if len(words) >= 2 and words[1] == 'hundred' and SMALL.get(words[0], 0) in range(1, 10):
        tail = ' '.join(words[2:]).removeprefix('and ')
        value = number(tail) if tail else 0
        return SMALL[words[0]] * 100 + value if value is not None and value < 100 else None
    if text in SMALL:
        return SMALL[text]
    if text in TENS:
        return TENS[text]
    if len(words) == 2 and words[0] in TENS and SMALL.get(words[1], 0) in range(1, 10):
        return TENS[words[0]] + SMALL[words[1]]
    return None


def duration(text):
    total = 0
    used = set()
    chunks = list(re.finditer(r'(.+?)\s+(second|minute|hour)s?(?=\s|$)', text))
    end = 0
    for match in chunks:
        if match.start() != end:
            return None
        value = number(match[1].strip().removeprefix('and '))
        unit = match[2]
        if value is None or unit in used:
            return None
        total += value * UNITS[unit]
        used.add(unit)
        end = match.end()
    return total if end == len(text) and 1 <= total < 86400 else None


def intent(text):
    """Return a typed action description, clarification, or None for chat."""
    # Preserve decimal and signed argument tokens for rejection before normalizing.
    invalid_numeric = re.search(r'\d[.,]\d|(?<!\w)[+-]\d|\b(?:minus|negative|point)\b', text.lower())
    words = normalize(text).removeprefix('please ').removesuffix(' please')
    words = re.sub(r'\bthe ', '', words).replace('a stopwatch', 'stopwatch').replace('a pomodoro', 'pomodoro')
    words = re.sub(r'^turn (?:your )?volume (up|down)\b', r'turn \1 volume', words)
    match = re.fullmatch(r'(?:set|start) (?:a )?(?:countdown )?timer(?: for)?(?: (.+))?', words)
    if match:
        seconds = None if invalid_numeric else duration(match[1] or '')
        return ('utility', 'timer', 'start', seconds) if seconds else ('clarify', 'Say a timer duration with units, for example ten minutes.')
    if words in ('how much time is left', 'how much time is left on timer', 'time remaining', 'timer status', 'how long is left on timer'):
        return ('remaining',)
    if words in ('stop talking', 'stop speaking', 'be quiet'):
        return ('quiet',)
    if words in ('what mode am i in', 'what mode are you in', 'current mode', 'operating mode',
                 'what is current operating mode', 'what is your current mode'):
        return ('mode_query',)
    if words in ('status', 'runtime status', 'how is system doing', 'what is your status'):
        return ('status',)
    match = re.fullmatch(r'(?:(?:enter|switch to|enable|go to) )?(standby|companion|sleep)(?: mode)?', words)
    if match:
        return ('mode', match[1])
    match = re.fullmatch(r'(increase|decrease|raise|lower|turn up|turn down) (?:your )?volume(?: by (.+))?', words)
    if match:
        raw = match[2]
        step = number(re.sub(r' percent(?:age points)?$', '', raw)) if raw else 10
        if invalid_numeric or step is None or not 1 <= step <= 100:
            return ('clarify', 'Say how many percentage points to change the volume.')
        return ('relative_volume', step if match[1] in ('increase', 'raise', 'turn up') else -step)
    match = re.fullmatch(r'(?:set )?(?:your )?volume (?:to )?(.+?)(?: percent)?', words)
    if match:
        value = None if invalid_numeric else number(match[1])
        return ('volume', value) if value is not None and 0 <= value <= 100 else ('clarify', 'Say a volume between zero and one hundred percent.')
    match = re.fullmatch(r'(start|stop|pause|resume|cancel) (?:my )?(timer|stopwatch|pomodoro)', words)
    if match:
        action, name = match.groups()
        if action == 'stop':
            action = {'timer': 'cancel', 'stopwatch': 'pause'}.get(name, 'pause')
        if action == 'cancel' and name != 'timer':
            return ('clarify', f'Say pause {name}.')
        return ('utility', name, action, None)
    if words in ('pause', 'resume', 'cancel', 'stop', 'start'):
        return ('clarify', 'Which utility would you like to control?')
    return None
