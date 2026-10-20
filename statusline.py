#!/usr/bin/env python3
"""Claude Code status line.

Shows the folder, session title, model and effort, context use, this instance's
share of the session usage, and session and weekly usage with reset times and
API-equivalent cost. Reads the status JSON on stdin.

Works with Python 3.8+ on macOS, Linux and Windows. Run
`python3 statusline.py --install` to point ~/.claude/settings.json at this file.
"""

import glob
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

HOME = os.path.expanduser('~')
ESC = '\x1b'
DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def paint(color, text):
    return '%s[%sm%s%s[0m' % (ESC, color, text, ESC)


DOT = paint('90', ' \u00b7 ')


def get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fixed(x, places=0):
    """x with a fixed number of decimals, rounding halves away from zero."""
    q = Decimal(1).scaleb(-places)
    return str(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


# Green under 50%, yellow under 80%, red above.
def heat(pct):
    if pct >= 80:
        return '91'
    if pct >= 50:
        return '93'
    return '92'


def bar(pct):
    n = min(10, max(0, int(math.floor(pct / 10 + 0.5))))
    return paint(heat(pct), '\u2588' * n) + paint('90', '\u2591' * (10 - n))


# 83400 -> 83k, 1000000 -> 1M, 1250000 -> 1.3M
def tokens(n):
    n = float(n)
    if n >= 1000000:
        s = fixed(n / 1000000, 1)
        return (s[:-2] if s.endswith('.0') else s) + 'M'
    if n >= 1000:
        return fixed(n / 1000) + 'k'
    return fixed(n)


def reset_time(epoch, with_day):
    t = datetime.fromtimestamp(int(num(epoch)))
    s = str(t.hour % 12 or 12)
    if t.minute:
        s += ':%02d' % t.minute
    s += 'am' if t.hour < 12 else 'pm'
    if with_day:
        s = DAYS[t.weekday()] + ' ' + s
    return s


# 2.345 -> 2.35%
def pct2(p):
    return fixed(p, 2) + '%'


# API-equivalent cost: what the usage would cost on API billing. Nothing is billed,
# so it is shown as an estimate (~$).
def usd(n):
    return paint('96', '~$' + fixed(n, 0 if n >= 100 else 2))


# One usage window: its usage %, when it resets, and the API-equivalent spend in it if known.
def limit(label, window, with_day, spent, places=0):
    pct = num(window['used_percentage'])
    f = [paint(heat(pct), fixed(pct, places) + '%')]
    if window.get('resets_at') is not None:
        f.append(paint('90', 'resets ' + reset_time(window['resets_at'], with_day)))
    if spent is not None:
        f.append(usd(spent))
    return paint('1;97', label + ':') + ' ' + DOT.join(f)


def load(path):
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save(path, data):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, separators=(',', ':'))
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# Session blurb: the name set with /rename, else the latest auto-generated
# title in the transcript. Only the tail of the transcript is read.
def blurb(j):
    if j.get('session_name'):
        return str(j['session_name'])
    path = j.get('transcript_path')
    if not path or not os.path.isfile(path):
        return None
    with open(path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 2 * 1024 * 1024))
        text = fh.read().decode('utf-8', 'ignore')
    hits = re.findall(r'"type":"(?:ai|custom)-title","(?:aiTitle|customTitle)":("(?:[^"\\]|\\.)*")', text)
    return json.loads(hits[-1]) if hits else None


def render(j):
    parts = []

    folder = get(j, 'workspace', 'current_dir') or j.get('cwd')
    if folder:
        folder = str(folder)
        same = folder.lower().startswith(HOME.lower()) if os.name == 'nt' else folder.startswith(HOME)
        if same:
            folder = '~' + folder[len(HOME):]
        parts.append(paint('1;94', folder))

    try:
        b = blurb(j)
    except Exception:
        b = None
    if b:
        b = str(b)
        if len(b) > 40:
            b = b[:39].rstrip() + '\u2026'
        parts.append(paint('3;97', b))

    model = get(j, 'model', 'display_name')
    if model:
        m = paint('1;96', str(model))
        effort = get(j, 'effort', 'level')
        if effort:
            m += ' ' + paint('95', str(effort))
        parts.append(m)

    ctx_pct = get(j, 'context_window', 'used_percentage')
    if ctx_pct is not None:
        pct = math.floor(num(ctx_pct) + 0.5)
        ctx = paint('90', 'ctx') + ' ' + bar(pct)
        size = get(j, 'context_window', 'context_window_size')
        if size:
            # Tokens in the window now; fall back to the percentage if usage is absent.
            u = get(j, 'context_window', 'current_usage')
            if u:
                used = num(u.get('input_tokens')) + num(u.get('cache_creation_input_tokens')) + num(u.get('cache_read_input_tokens'))
            else:
                used = num(size) * num(ctx_pct) / 100
            ctx += ' ' + paint(heat(pct), tokens(used)) + paint('90', '/' + tokens(size))
        parts.append(ctx + ' ' + paint(heat(pct), '%d%%' % pct))

    # Usage is account-wide, but each instance only knows the numbers from its own
    # last API response. Instances share the newest numbers through a small cache
    # so an idle instance's line keeps up (with statusLine.refreshInterval re-running it).
    cache_path = os.environ.get('CLAUDE_STATUSLINE_CACHE') or os.path.join(HOME, '.claude', 'statusline-usage.json')
    now = int(time.time())

    def live(w):
        return (isinstance(w, dict) and w.get('used_percentage') is not None
                and w.get('resets_at') is not None and num(w['resets_at']) > now)

    # Each instance also keeps its own file under statusline-sessions: its
    # API-equivalent cost, that cost when each usage window began, and when its
    # usage numbers last changed.
    sess_dir = os.path.join(os.path.dirname(cache_path), 'statusline-sessions')
    sid = str(j.get('session_id') or '')
    me_path = os.path.join(sess_dir, sid + '.json') if re.match(r'^[\w-]+$', sid) else None
    me = (load(me_path) if me_path else None) or {}
    cost = num(get(j, 'cost', 'total_cost_usd'))

    # An instance's numbers count as observed when they last changed, so an idle
    # instance's old numbers lose to a busier instance's newer ones.
    rate_limits = j.get('rate_limits')
    sig = json.dumps(rate_limits, sort_keys=True, separators=(',', ':'))
    observed = int(num(me.get('observed'))) if me.get('sig') == sig else now

    def reading(w):
        if live(w):
            return {'used_percentage': num(w['used_percentage']), 'resets_at': int(num(w['resets_at'])), 'observed': observed}
        return None

    # Newer of two readings: the later window wins, else the later-observed reading.
    def newer(a, b):
        if not live(a):
            return b if live(b) else a
        if not live(b):
            return a
        gap = num(a['resets_at']) - num(b['resets_at'])
        if gap > 60:
            return a
        if gap < -60:
            return b
        return b if num(b.get('observed')) > num(a.get('observed')) else a

    cache = load(cache_path)
    if not isinstance(cache, dict):
        cache = {}
    five = newer(reading(get(rate_limits, 'five_hour')), cache.get('five_hour'))
    seven = newer(reading(get(rate_limits, 'seven_day')), cache.get('seven_day'))

    merged = {}
    for key, w in (('five_hour', five), ('seven_day', seven)):
        if live(w):
            merged[key] = {'used_percentage': num(w['used_percentage']), 'resets_at': int(num(w['resets_at'])),
                           'observed': int(num(w.get('observed')))}
    if merged and merged != cache:
        save(cache_path, merged)

    # Spend in a window: each instance's cost now minus its cost when the window
    # began. This instance's share of the window's usage % is its share of the spend.
    others = []
    if me_path:
        for path in glob.glob(os.path.join(sess_dir, '*.json')):
            if os.path.splitext(os.path.basename(path))[0] == sid:
                continue
            s = load(path)
            if not isinstance(s, dict):
                continue
            if num(s.get('seen')) < now - 8 * 86400:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            others.append(s)

    # A resumed instance's cost can restart lower; its earlier spend still counts.
    prev = num(me.get('cost'))
    shift = 0.0
    if cost < prev:
        shift = prev - cost
        prev = cost

    def same_window(e, w):
        return isinstance(e, dict) and abs(num(e.get('at')) - num(w['resets_at'])) <= 60

    def spend(key, w):
        e = me.get(key)
        base = num(e.get('base')) - shift if same_window(e, w) else prev
        mine = cost - base
        total = mine
        for s in others:
            o = s.get(key)
            if same_window(o, w):
                total += num(s.get('cost')) - num(o.get('base'))
        share = num(w['used_percentage']) * mine / total if total > 0 else 0.0
        return {'base': base, 'all': total, 'share': share}

    five_spend = seven_spend = None
    if me_path:
        state = {'cost': cost, 'seen': now, 'sig': sig, 'observed': observed}
        if live(five):
            five_spend = spend('five_hour', five)
            state['five_hour'] = {'at': int(num(five['resets_at'])), 'base': five_spend['base']}
        if live(seven):
            seven_spend = spend('seven_day', seven)
            state['seven_day'] = {'at': int(num(seven['resets_at'])), 'base': seven_spend['base']}
        try:
            os.makedirs(sess_dir, exist_ok=True)
        except OSError:
            pass
        save(me_path, state)

    # This instance: its share of the session window's usage, and its API-equivalent cost.
    has_cost = get(j, 'cost', 'total_cost_usd') is not None
    own = []
    if me_path:
        p5 = five_spend['share'] if five_spend else 0.0
        own.append(paint(heat(p5), pct2(p5)))
    if has_cost:
        own.append(usd(cost))
    if own:
        parts.append(paint('1;97', 'inst:') + ' ' + DOT.join(own))

    # Account-wide windows, with what all instances here spent in each.
    def spent(sp):
        if not (has_cost and me_path):
            return None
        return sp['all'] if sp else 0.0

    if live(five):
        parts.append(limit('session', five, False, spent(five_spend), 1))
    else:
        parts.append(paint('1;97', 'session:') + ' ' + paint('92', '0.0%'))  # no active window means zero usage

    if live(seven):
        parts.append(limit('week', seven, True, spent(seven_spend)))

    return paint('90', ' \u2502 ').join(parts)


def install():
    """Point ~/.claude/settings.json's statusLine at this file, keeping a .bak copy."""
    settings = os.path.join(HOME, '.claude', 'settings.json')
    data = {}
    if os.path.exists(settings):
        with open(settings, encoding='utf-8-sig') as fh:
            data = json.load(fh)
        shutil.copyfile(settings, settings + '.bak')
    script = os.path.abspath(__file__).replace('\\', '/')
    if os.name == 'nt':
        cmd = '"%s" "%s"' % (sys.executable.replace('\\', '/'), script)
    else:
        cmd = 'python3 "%s"' % script
    line = data.get('statusLine') if isinstance(data.get('statusLine'), dict) else {}
    line.update(type='command', command=cmd)
    data['statusLine'] = line
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    tmp = settings + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)
        fh.write('\n')
    os.replace(tmp, settings)
    print('statusLine command set in %s:\n  %s' % (settings, cmd))


def main():
    if '--install' in sys.argv[1:]:
        install()
        return
    try:
        j = json.loads(sys.stdin.buffer.read().decode('utf-8-sig') or '{}')
    except ValueError:
        j = {}
    if not isinstance(j, dict):
        j = {}
    sys.stdout.buffer.write(render(j).encode('utf-8'))
    sys.stdout.flush()


if __name__ == '__main__':
    main()
