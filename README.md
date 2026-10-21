# claude-statusline

A [Claude Code](https://claude.com/claude-code) status line for macOS, Linux and Windows (any shell: bash, zsh, fish, PowerShell, cmd).

```
~/proj │ Fix the login bug │ Opus 5.5 high │ ctx █░░░░░░░░░ 83k/1M 8% │ inst: 7.68% · ~$3.00 │ session: 16.0% · resets 12:59pm · ~$6.25 │ week: 23% · resets Fri 9:59am · ~$6.25
```

- **inst:** this Claude Code instance's share of the session usage, and its API-equivalent cost.
- **session / week:** account-wide usage, reset time, and API-equivalent cost of all instances on this machine.
- `~$` amounts are what the usage would cost on API billing. Nothing is billed on a subscription.

## Install

Needs Python 3.8+ (on macOS, `python3` comes with the Xcode Command Line Tools, which `git` also needs).

macOS / Linux:

```sh
git clone https://github.com/johnjp15/claude-statusline.git ~/claude-statusline
python3 ~/claude-statusline/statusline.py --install
```

Windows:

```sh
git clone https://github.com/johnjp15/claude-statusline.git %USERPROFILE%\claude-statusline
python %USERPROFILE%\claude-statusline\statusline.py --install
```

`--install` sets `statusLine` in `~/.claude/settings.json` (a copy is kept as `settings.json.bak`), including a 30-second
`refreshInterval` so idle instances keep up with usage from other instances. Restart Claude Code to see it.

Update with `git pull`; no reinstall needed.

## How the per-instance numbers work

Usage limits are account-wide, so each instance writes its API-equivalent cost to
`~/.claude/statusline-sessions/<session id>.json`. An instance's share of the session usage is
its share of the cost all instances on this machine spent in that window. Usage from claude.ai or
other machines is counted toward the instances here, so shares are estimates.
