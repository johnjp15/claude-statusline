# claude-statusline

A [Claude Code](https://claude.com/claude-code) status line for macOS, Linux and Windows (any shell: bash, zsh, fish, PowerShell, cmd).

```
~/proj │ Fix the login bug │ Opus 5.5 high │ ctx █░░░░░░░░░ 83k/1M 8% │ 💬 7.7% · 3.84% · ~$3.00 │ ⏳ 16.0% · ~$6.25 · ↻ 12:59pm │ 📅 23.00% · ~$6.25 · ↻ Fri 9:59am
```

- **💬 this instance:** its share of the session usage and of the week usage, and its API-equivalent cost.
- **⏳ session / 📅 week:** account-wide usage, API-equivalent cost of all instances on this machine, and reset time (↻).
- Each percentage is colored by its own value every 20%: session numbers run turquoise → teal → rose → coral → red,
  week numbers lavender → orchid → magenta → pink → red. The instance's two shares use the same scales, so color tells
  you which window each one belongs to.
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
