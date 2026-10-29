# releaseclone

Watches GitHub, GitLab and Gitea/Forgejo (Codeberg etc.) repos for new releases and downloads
the assets you choose. Python 3.11+, standard library only (the TUI uses curses).

## Requirements

- **Python 3.11 or newer**, needed for `tomllib`. Nothing to `pip install`: it uses only the standard library.
- **Linux, macOS or another Unix.** The TUI needs Python's `curses` module, which most Linux
  distributions and macOS include. On Windows the command-line part should work, but the TUI
  would need `pip install windows-curses`, and none of it has been tested there.
- Optional:
  - `notify-send` (libnotify) for desktop notifications
  - `xdg-open` (xdg-utils) for the TUI's "open folder"
  - cron, if you want scheduled checks without leaving `watch` running

## Install

```sh
git clone https://github.com/zythros/releaseclone
cd releaseclone
./install.sh
```

This checks the requirements and copies the program to `~/.local/lib/releaseclone`. It also
puts a `releaseclone` command in `~/.local/bin` and creates `~/.config/releaseclone/config.toml`
from the example, unless you already have one. Run it again after `git pull` to update.

- `./install.sh --prefix /some/dir` installs somewhere else.
- `./install.sh --uninstall` removes the program. Your config, state and downloads are kept.

You can also skip installing and run `./releaseclone.py` straight from the checkout.

## Setup

Edit `~/.config/releaseclone/config.toml`, or add repos interactively with `releaseclone tui`.
Find the right `include` patterns by previewing a repo's latest release (`*` marks a match):

```sh
releaseclone list tangency-labs/solidcraft -i 'solidcraft-*-linux-x86_64.AppImage'
```

## Interactive TUI

```sh
releaseclone tui
```

If you have no config yet, it creates one. (Running from a checkout, `releaseclone_tui.py` must sit next to `releaseclone.py`.)

| Key | Action |
|---|---|
| `a` | add a repo: paste a URL, tick the files you want, confirm |
| `p` | pick files again from the latest release (patterns are generated for you) |
| `e` | edit a repo (patterns, keep, prereleases, destination, …) |
| `⏎` | downloaded releases: `d` delete, `g` download again, `o` open folder |
| `r` / `R` | check selected / all repos now |
| `s` | global settings (folder, interval, keep, notifications) |
| `o` | open the repo's download folder |
| `d` | remove a repo (optionally with its files) |
| `l` / `L` | toggle the log pane / full-screen log |
| `A` | toggle auto-checking |
| `q` | quit |

While the TUI is open it checks repos on the configured interval, like `watch`. It can run
alongside a background `watch`; a lock file keeps them from overwriting each other's state.
Edits rewrite only the setting or `[[repo]]` block you changed. Comments elsewhere are kept,
and the original is saved as `config.toml.bak`.

## Command line

```sh
releaseclone run       # check all repos once
releaseclone watch     # keep running, checking every `interval`
releaseclone status    # what's been downloaded
```

Files go to `<dest>/<owner_repo>/<tag>/`, and a `latest` symlink points at the newest one.

- On the first check of a repo, only its newest release is downloaded (no backfill).
- Downloads are verified against GitHub's asset digests and any `SHA256SUMS`/`checksums` file.
- A failed release is retried on the next check.
- Rate limits are honoured. If a site says the limit is used up (`429`, or `Retry-After` /
  `X-RateLimit-*` headers), that site isn't contacted again until its reset time. Unauthenticated
  GitHub allows 60 requests/hour; set `GITHUB_TOKEN` to get 5000.
- `.AppImage` files are made executable.
- `keep = N` keeps the newest N releases and deletes older ones. Set it globally or per `[[repo]]`;
  0 keeps everything. It's applied on every check, so lowering it takes effect right away.
  `run --keep N` / `watch --keep N` override it for all repos.

State is kept in `~/.local/share/releaseclone/state.json`.

## Running periodically

Use **either** cron (`crontab -e`):

```
0 */6 * * * ~/.local/bin/releaseclone run >> ~/.local/share/releaseclone/log 2>&1
```

**or** start `releaseclone watch` from your session autostart, or as a user service.
`watch` re-reads the config before each check, so edits apply without a restart.

## License

MIT, see [LICENSE](LICENSE).
