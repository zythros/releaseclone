# releaseclone

Watches GitHub, GitLab and Gitea/Forgejo (Codeberg etc.) repos for new releases and downloads
the assets you choose. Python 3.11+, standard library only (the TUI uses curses).

## Setup

```sh
mkdir -p ~/.config/releaseclone
cp config.example.toml ~/.config/releaseclone/config.toml
$EDITOR ~/.config/releaseclone/config.toml
```

Find the right `include` patterns by previewing a repo's latest release (`*` marks a match):

```sh
./releaseclone.py list tangency-labs/solidcraft -i 'solidcraft-*-linux-x86_64.AppImage'
```

## Interactive TUI

```sh
./releaseclone.py tui
```

Needs `releaseclone_tui.py` next to `releaseclone.py`. If you have no config yet, it creates one.

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
./releaseclone.py run       # check all repos once
./releaseclone.py watch     # keep running, checking every `interval`
./releaseclone.py status    # what's been downloaded
```

Files go to `<dest>/<owner_repo>/<tag>/`, and a `latest` symlink points at the newest one.

- On the first check of a repo, only its newest release is downloaded (no backfill).
- Downloads are verified against GitHub's asset digests and any `SHA256SUMS`/`checksums` file.
- A failed release is retried on the next check.
- `.AppImage` files are made executable.
- `keep = N` keeps the newest N releases and deletes older ones. Set it globally or per `[[repo]]`;
  0 keeps everything. It's applied on every check, so lowering it takes effect right away.
  `run --keep N` / `watch --keep N` override it for all repos.

State is kept in `~/.local/share/releaseclone/state.json`.

## Running periodically

Use **either** cron (`crontab -e`):

```
0 */6 * * * /path/to/releaseclone.py run >> ~/.local/share/releaseclone/log 2>&1
```

**or** start `releaseclone.py watch` from your session autostart, or as a user service.
`watch` re-reads the config before each check, so edits apply without a restart.
