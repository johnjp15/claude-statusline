#!/usr/bin/env python3
"""Claude Code status line.

Shows the folder and git branch, session title, model and effort, context use
(with a /compact nudge), prompt cache hit ratio, time until it goes cold and miss
causes, uncommitted lines changed and session duration, then this instance's
share of the session usage, and session and weekly usage with reset times and
API-equivalent cost, plus the weekly pace. The session parts are left out when
there is no 5-hour window (plans without a session limit, such as Ultra).
Reads the status JSON on stdin.

Works with Python 3.8+ on macOS, Linux and Windows. Run
`python3 statusline.py --install` to point ~/.claude/settings.json at this file.
"""

import glob
import json
import math
import os
import re
import shutil
import subprocess
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


# Usage windows step through their own ramp every 20%, ending in red, so session and
# week stay apart while both warn as they fill. Green is kept for costs.
SESSION_RAMP = (80, 73, 168, 203, 196)  # turquoise to red through rose
WEEK_RAMP = (183, 177, 170, 205, 197)  # lavender to red through orchid and magenta


def usage_heat(pct, ramp):
    return '38;5;%d' % ramp[min(4, int(max(0.0, pct) // 20))]


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


# 45000 -> 45s, 750000 -> 12m, 4320000 -> 1h12m
def duration(ms):
    s = int(num(ms) // 1000)
    if s < 60:
        return '%ds' % s
    m = s // 60
    if m < 60:
        return '%dm' % m
    return '%dh%02dm' % (m // 60, m % 60)


# API-equivalent cost: what the usage would cost on API billing. Nothing is billed,
# so it is shown as an estimate (~$).
def usd(n):
    return paint('92', '~$' + fixed(n, 0 if n >= 100 else 2))


# Segment icons: this instance (its conversation), the 5-hour session window (an hourglass), the week (a calendar).
# All are emoji by default; ones that need VS16 to become emoji throw off terminal width.
INST_ICON = '\U0001f4ac'
SESSION_ICON = '\u23f3'
WEEK_ICON = '\U0001f4c5'


# Where a window's usage is headed: the % at reset if the pace so far holds, or
# when it would hit 100% if that comes first. Skipped early in the window, when
# there is too little to go on.
def pace(window, length, ramp, with_day):
    used = num(window['used_percentage'])
    start = num(window['resets_at']) - length
    elapsed = time.time() - start
    if not 0 < used < 100 or elapsed < length / 20:
        return None
    proj = used * length / elapsed
    if proj >= 100:
        return paint(usage_heat(100, ramp), 'hits 100% ~' + reset_time(start + elapsed * 100 / used, with_day))
    return paint(usage_heat(proj, ramp), 'pace ' + fixed(proj) + '%')


# One usage window: its usage %, pace if the window's length is given, the
# API-equivalent spend in it if known, and when it resets.
def limit(label, ramp, window, with_day, spent, places=0, length=None):
    pct = num(window['used_percentage'])
    f = [paint(usage_heat(pct, ramp), fixed(pct, places) + '%')]
    p = pace(window, length, ramp, with_day) if length else None
    if p:
        f.append(p)
    if spent is not None:
        f.append(usd(spent))
    if window.get('resets_at') is not None:
        f.append(paint('90', '\u21bb ' + reset_time(window['resets_at'], with_day)))
    return paint('1;97', label) + ' ' + DOT.join(f)


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


EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'  # git's id for an empty tree


# Git state of a folder, or None outside a repo: the branch with * when the
# working tree has changes (a detached HEAD shows its short commit id), and the
# lines added and removed by uncommitted changes, untracked files included.
def git_state(folder):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')  # don't take index locks a running git might need

    def git(*args):
        r = subprocess.run(['git', '-C', folder] + list(args), capture_output=True, timeout=2, env=env)
        return r.stdout.decode('utf-8', 'ignore') if r.returncode == 0 else None

    try:
        out = git('status', '--porcelain=v2', '--branch', '--untracked-files=all', '-z')
        if out is None:
            return None
        head = oid = None
        changed = False
        untracked = []
        for e in out.split('\0'):
            if e.startswith('# branch.head '):
                head = e[len('# branch.head '):]
            elif e.startswith('# branch.oid '):
                oid = e[len('# branch.oid '):]
            elif e.startswith('? '):
                untracked.append(e[2:])
            elif e and not e.startswith('#'):
                changed = True
        initial = not oid or oid == '(initial)'
        if head == '(detached)':
            head = None if initial else oid[:7]
        if not head:
            return None

        added = removed = 0
        if changed:
            for line in (git('diff', EMPTY_TREE if initial else 'HEAD', '--numstat') or '').splitlines():
                a, r, _ = line.split('\t', 2)
                if a != '-':  # binary files show - for both counts
                    added += int(a)
                    removed += int(r)
        # Untracked files count as all added. Paths are relative to the repo root;
        # binary and large files are skipped.
        root = (git('rev-parse', '--show-toplevel') or '').strip() if untracked else ''
        for path in untracked[:200] if root else []:
            p = os.path.join(root, path)
            try:
                if os.path.getsize(p) > 1024 * 1024:
                    continue
                with open(p, 'rb') as fh:
                    data = fh.read()
            except OSError:
                continue
            if b'\0' not in data:
                added += data.count(b'\n') + (1 if data and not data.endswith(b'\n') else 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    dirty = changed or bool(untracked)
    return {'branch': head + ('*' if dirty else ''), 'added': added, 'removed': removed}


# Short names for the likely cache-miss causes Claude Code reports.
CAUSES = {'tools_changed': 'tools changed', 'system_prompt_changed': 'system prompt changed',
          'ttl_expired_5m': '5m TTL expired', 'likely_server_side': 'server side'}


# The session's cache-miss causes, most frequent first, e.g. "tools changed ×2,
# server side". Older Claude Code versions report only the last miss's causes.
def miss_causes(pc):
    counts = pc.get('miss_causes')
    if not isinstance(counts, dict) or not counts:
        last = get(pc, 'last_miss_cause', 'causes')
        counts = dict.fromkeys(last, 1) if isinstance(last, list) else {}
    out = []
    for k, n in sorted(counts.items(), key=lambda kv: -num(kv[1])):
        s = CAUSES.get(k, str(k).replace('_', ' '))
        if num(n) > 1:
            s += ' ×%d' % num(n)
        out.append(s)
    return ', '.join(out)


def render(j):
    parts = []

    g = None
    folder = get(j, 'workspace', 'current_dir') or j.get('cwd')
    if folder:
        folder = str(folder)
        g = git_state(folder)
        same = folder.lower().startswith(HOME.lower()) if os.name == 'nt' else folder.startswith(HOME)
        if same:
            folder = '~' + folder[len(HOME):]
        f = paint('1;94', folder)
        if g:
            f += ' ' + paint('35', '(' + g['branch'] + ')')
        parts.append(f)

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
        ctx += ' ' + paint(heat(pct), '%d%%' % pct)
        if pct >= 70:  # compact by choice before auto-compact does it mid-task
            ctx += ' ' + paint(heat(pct), '→ /compact')
        parts.append(ctx)

    # Prompt cache: the session's hit ratio, time until the cache goes cold (the
    # next message then re-caches the context at full price), and misses.
    pc = j.get('prompt_cache')
    if isinstance(pc, dict) and pc.get('caching_observed'):
        c = []
        if pc.get('hit_ratio') is not None:
            h = num(pc['hit_ratio']) * 100
            c.append(paint('92' if h >= 80 else '93' if h >= 50 else '91', fixed(h) + '%'))
        left = num(pc['expires_at']) - time.time() if pc.get('expires_at') is not None else 0
        if left > 0:
            frac = left / (3600 if pc.get('ttl') == '1h' else 300)
            c.append(paint('92' if frac > 1 / 3 else '93' if frac > 1 / 12 else '91',
                           'warm ' + ('%dm' % (left // 60) if left >= 60 else '%ds' % left)))
        else:
            c.append(paint('91', 'cold'))
            if pc.get('recache_tokens_if_cold'):
                c.append(paint('91', 'recache ' + tokens(pc['recache_tokens_if_cold'])))
        misses = int(num(pc.get('misses')))
        if misses:
            m = '%d miss%s' % (misses, '' if misses == 1 else 'es')
            why = miss_causes(pc)
            if why:
                m += ' (' + why + ')'
            c.append(paint('93', m))
        parts.append(paint('90', 'cache') + ' ' + DOT.join(c))

    if g and (g['added'] or g['removed']):
        parts.append(paint('92', '+%d' % g['added']) + ' ' + paint('91', '−%d' % g['removed']))

    ms = get(j, 'cost', 'total_duration_ms')
    if ms is not None:
        parts.append(paint('37', duration(ms)))

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
    # numbers for each window last changed.
    sess_dir = os.path.join(os.path.dirname(cache_path), 'statusline-sessions')
    sid = str(j.get('session_id') or '')
    me_path = os.path.join(sess_dir, sid + '.json') if re.match(r'^[\w-]+$', sid) else None
    me = (load(me_path) if me_path else None) or {}
    cost = num(get(j, 'cost', 'total_cost_usd'))

    # A window's numbers count as observed when this instance's numbers for it last
    # changed, so an idle instance's old numbers lose to a busier instance's newer
    # ones. Windows are tracked apart: Claude Code drops a window once it resets,
    # and that must not make the other window's old numbers look new.
    rate_limits = j.get('rate_limits')
    seen = me.get('obs') if isinstance(me.get('obs'), dict) else {}
    obs = {}

    def reading(key):
        w = get(rate_limits, key)
        if not live(w):
            return None
        pct, at = num(w['used_percentage']), int(num(w['resets_at']))
        e = seen.get(key)
        same = isinstance(e, dict) and num(e.get('pct')) == pct and int(num(e.get('at'))) == at
        obs[key] = {'pct': pct, 'at': at, 't': int(num(e['t'])) if same else now}
        return {'used_percentage': pct, 'resets_at': at, 'observed': obs[key]['t']}

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
    five = newer(reading('five_hour'), cache.get('five_hour'))
    seven = newer(reading('seven_day'), cache.get('seven_day'))

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

    # Only usage that grew while instances here were tracked is shared out, so usage
    # from before they started (or before this fix) isn't pinned on them. p0 is the
    # window's % when an instance first saw it: 0 for a window that began while the
    # instance was already running, since its base cost is then from the window start.
    def spend(key, w):
        pct = num(w['used_percentage'])
        e = me.get(key)
        if same_window(e, w) and 'p0' in e:
            base = num(e.get('base')) - shift
            p0 = num(e['p0'])
        elif same_window(e, w):  # tracked before p0 existed: start counting now
            base, p0 = prev, pct
        else:
            base = prev
            p0 = 0.0 if me.get('seen') else pct
        mine = cost - base
        total = mine
        start = p0
        for s in others:
            o = s.get(key)
            if same_window(o, w) and 'p0' in o:
                total += num(s.get('cost')) - num(o.get('base'))
                start = min(start, num(o['p0']))
        grown = max(0.0, pct - start)
        share = grown * mine / total if total > 0 else 0.0
        return {'base': base, 'p0': p0, 'all': total, 'share': share}

    five_spend = seven_spend = None
    if me_path:
        state = {'cost': cost, 'seen': now, 'obs': obs}
        if live(five):
            five_spend = spend('five_hour', five)
            state['five_hour'] = {'at': int(num(five['resets_at'])), 'base': five_spend['base'], 'p0': five_spend['p0']}
        if live(seven):
            seven_spend = spend('seven_day', seven)
            state['seven_day'] = {'at': int(num(seven['resets_at'])), 'base': seven_spend['base'], 'p0': seven_spend['p0']}
        try:
            os.makedirs(sess_dir, exist_ok=True)
        except OSError:
            pass
        save(me_path, state)

    # This instance: its share of the session and week usage, and its API-equivalent cost.
    # Each share is colored on its own window's ramp.
    has_cost = get(j, 'cost', 'total_cost_usd') is not None
    own = []
    if me_path:
        if five_spend:
            p5 = five_spend['share']
            own.append(paint(usage_heat(p5, SESSION_RAMP), fixed(p5, 1) + '%'))
        if seven_spend:
            p7 = seven_spend['share']
            own.append(paint(usage_heat(p7, WEEK_RAMP), fixed(p7, 1) + '%'))
    if has_cost:
        own.append(usd(cost))
    line1 = parts
    parts = []
    if own:
        parts.append(paint('1;97', INST_ICON) + ' ' + DOT.join(own))

    # Account-wide windows, with what all instances here spent in each.
    def spent(sp):
        if not (has_cost and me_path):
            return None
        return sp['all'] if sp else 0.0

    # Claude Code reports window usage in whole percents, so it is shown without decimals.
    # Plans without a session limit (Ultra) get no five_hour window, so the session
    # segment only appears while a window is live. On plans that have one, it is
    # hidden between a reset and the next message, when usage would be 0%.
    if live(five):
        parts.append(limit(SESSION_ICON, SESSION_RAMP, five, False, spent(five_spend)))

    if live(seven):
        parts.append(limit(WEEK_ICON, WEEK_RAMP, seven, True, spent(seven_spend), length=7 * 86400))

    sep = paint('90', ' \u2502 ')
    return sep.join(line1) + '\n' + sep.join(parts)


def install():
    """Point ~/.claude/settings.json's statusLine at this file, keeping a .bak copy.

    Also sets a 30s refreshInterval unless one is set, so idle instances pick up
    usage from busier ones.
    """
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
    line.setdefault('refreshInterval', 30)
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
