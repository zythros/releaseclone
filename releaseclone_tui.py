"""Terminal UI for releaseclone, started with `releaseclone.py tui`. Uses only stdlib curses.

Checks and downloads run on a background worker thread; the UI renders a snapshot of the
state file that the worker publishes after each job. Config edits rewrite only the
[[repo]] block or top-level key being changed, so comments elsewhere survive.
"""

from __future__ import annotations

import copy
import curses
import glob
import json
import queue
import random
import re
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.parse
from collections import deque
from dataclasses import dataclass
from pathlib import Path

core = None  # the releaseclone module, injected by run()

NEW_CONFIG = """\
# releaseclone config (created by the TUI). See config.example.toml for every option.
dest = "~/Downloads/releases"
interval = "6h"
keep = 3
"""

GREEN, YELLOW, RED, CYAN = 1, 2, 3, 4


# --------------------------------------------------------------------------- config writing


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # JSON strings are valid TOML basic strings


REPO_KEY_ORDER = ["url", "provider", "include", "exclude", "keep", "prereleases", "dest", "token", "post_command"]
TABLE_RE = re.compile(r"^\[\[?\s*[A-Za-z_]")


class ConfigFile:
    """Edits config.toml in place, one [[repo]] block or top-level key at a time."""

    def __init__(self, path: Path):
        self.path = path
        self.backed_up = False

    def read(self) -> str:
        return self.path.read_text() if self.path.exists() else ""

    def raw(self) -> dict:
        return tomllib.loads(self.read())

    @staticmethod
    def blocks(text: str) -> list[list[str]]:
        """Split into [top-level, table, table, ...], each a list of lines."""
        out: list[list[str]] = [[]]
        for line in text.splitlines(keepends=True):
            if TABLE_RE.match(line):
                out.append([])
            out[-1].append(line)
        return out

    @staticmethod
    def split_tail(lines: list[str]) -> tuple[list[str], list[str]]:
        """Separate trailing blank/comment lines, which belong to whatever follows."""
        i = len(lines)
        while i and (not lines[i - 1].strip() or lines[i - 1].lstrip().startswith("#")):
            i -= 1
        return lines[:i], lines[i:]

    def find_repo(self, blocks, url: str) -> int | None:
        for i, b in enumerate(blocks[1:], 1):
            if b[0].startswith("[[repo]]"):
                try:
                    r = tomllib.loads("".join(b))["repo"][0]
                except (tomllib.TOMLDecodeError, KeyError, IndexError):
                    continue
                if r.get("url", "").rstrip("/") == url.rstrip("/"):
                    return i
        return None

    @staticmethod
    def repo_lines(data: dict) -> list[str]:
        keys = [k for k in REPO_KEY_ORDER if k in data] + [k for k in data if k not in REPO_KEY_ORDER]
        return ["[[repo]]\n"] + [f"{k} = {toml_value(data[k])}\n" for k in keys if data[k] is not None]

    def save_repo(self, old_url: str | None, data: dict) -> None:
        blocks = self.blocks(self.read())
        new = self.repo_lines(data)
        idx = self.find_repo(blocks, old_url) if old_url else None
        if idx is not None:
            _, tail = self.split_tail(blocks[idx])
            blocks[idx] = new + tail
        else:
            repo_idx = [i for i, b in enumerate(blocks) if i and b[0].startswith("[[repo]]")]
            if repo_idx:  # after the last repo, before any trailing comments
                body, tail = self.split_tail(blocks[repo_idx[-1]])
                blocks[repo_idx[-1]] = body + ["\n"] + new + tail
            else:
                if blocks[-1] and not blocks[-1][-1].endswith("\n"):
                    blocks[-1][-1] += "\n"
                blocks.append(["\n"] + new)
        self.write("".join("".join(b) for b in blocks))

    def delete_repo(self, url: str) -> None:
        blocks = self.blocks(self.read())
        idx = self.find_repo(blocks, url)
        if idx is not None:
            blocks[idx] = self.split_tail(blocks[idx])[1]
            self.write("".join("".join(b) for b in blocks))

    def set_top(self, key: str, value) -> None:
        blocks = self.blocks(self.read())
        top = blocks[0]
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
        idx = next((i for i, l in enumerate(top) if pat.match(l)), None)
        if idx is not None:
            if value is None:
                del top[idx]
            else:
                comment = re.search(r"(\s+#[^\"']*)$", top[idx].rstrip("\n"))
                top[idx] = f"{key} = {toml_value(value)}{comment[1] if comment else ''}\n"
        elif value is not None:
            body, tail = self.split_tail(top)
            blocks[0] = body + [f"{key} = {toml_value(value)}\n"] + tail
        self.write("".join("".join(b) for b in blocks))

    def write(self, text: str) -> None:
        tomllib.loads(text)  # never write a config we couldn't read back
        if self.path.exists() and not self.backed_up:
            shutil.copy2(self.path, self.path.with_suffix(".toml.bak"))
            self.backed_up = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self.path)


# --------------------------------------------------------------------------- cron

CRON_TAG = "# releaseclone"  # marks the one crontab line we own
CRON_HOURS = (1, 2, 3, 4, 6, 8, 12, 24)  # intervals cron can space evenly through a day


class Crontab:
    """Reads and edits the user's crontab, touching only the line tagged CRON_TAG."""

    @staticmethod
    def available() -> bool:
        return shutil.which("crontab") is not None

    @staticmethod
    def lines() -> list[str]:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.splitlines()
        if "no crontab" in r.stderr.lower():
            return []
        # Anything else (permissions, cron missing): never risk overwriting the crontab blind.
        raise RuntimeError(f"can't read crontab: {r.stderr.strip() or r.returncode}")

    @classmethod
    def ours(cls) -> str | None:
        return next((l for l in cls.lines() if l.rstrip().endswith(CRON_TAG)), None)

    @classmethod
    def set(cls, hours: int | None, command: str) -> None:
        """Install/update our line to run every `hours` hours; None removes it."""
        if not cls.available():
            raise RuntimeError("the crontab command isn't installed")
        lines = cls.lines()
        old = next((l for l in lines if l.rstrip().endswith(CRON_TAG)), None)
        lines = [l for l in lines if l is not old]
        if hours:
            # Keep the existing minute; otherwise pick one at random so we don't all hit :00.
            minute = old.split()[0] if old and old.split()[0].isdigit() else str(random.randint(1, 59))
            hour_field = "*" if hours == 1 else ("0" if hours == 24 else f"*/{hours}")
            lines.append(f"{minute} {hour_field} * * * {command} {CRON_TAG}")
        if lines == cls.lines():
            return
        r = subprocess.run(["crontab", "-"], input="".join(l + "\n" for l in lines),
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"crontab refused the update: {r.stderr.strip()}")


# --------------------------------------------------------------------------- helpers


def cursor(visible: int) -> None:
    try:
        curses.curs_set(visible)
    except curses.error:
        pass  # terminal can't hide/show the cursor


def generalize(name: str, tag: str) -> str:
    """Turn an asset name into a glob that survives version bumps: foo-1.2-x.tgz -> foo-*-x.tgz"""
    for ver in (tag.lstrip("vV"), tag):
        if len(ver) >= 2 and re.search(r"\d", ver) and ver in name:
            return "*".join(glob.escape(p) for p in name.split(ver))
    return glob.escape(name)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60:.0f}m ago"
    if d < 86400:
        return f"{d // 3600:.0f}h ago"
    return f"{d // 86400:.0f}d ago"


def dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.iterdir() if f.is_file())
    except OSError:
        return 0


@dataclass
class Field:
    label: str
    key: str
    value: object
    kind: str  # text | list | int | tri | bool | hours
    help: str = ""

    def show(self) -> str:
        v = self.value
        if self.kind == "tri":
            return {None: "(default)", True: "yes", False: "no"}[v]
        if self.kind == "bool":
            return "on" if v else "off"
        if self.kind == "hours" and v:
            return f"{v} hour{'s' if v != 1 else ''}"
        if v is None or v == "":
            return "(default)"
        if self.kind == "list":
            return ", ".join(v) if v else "(none: nothing will be downloaded)"
        return str(v)


# --------------------------------------------------------------------------- app


class App:
    def __init__(self, scr, args):
        self.scr = scr
        self.args = args
        self.cfgfile = ConfigFile(args.config)
        self.cfg: dict = {}
        self.repos: list = []
        self.cfg_error = ""
        self.cfg_mtime = None
        self.logs: deque[str] = deque(maxlen=1000)
        self.live: dict[str, str] = {}  # repo key -> status text while the worker is on it
        self.snapshot: dict = {}
        self.snap_lock = threading.Lock()
        self.jobs: queue.Queue = queue.Queue()
        self.queued: set[str] = set()
        self.busy: str | None = None
        self.current_key: str | None = None
        self.sel = 0
        self.top = 0
        self.msg = ("", 0.0)
        self.show_log = True
        self.auto = True
        core._log_sink = self.logs.append
        core._progress_sink = self.on_progress
        self.publish(core.State(args.state))
        threading.Thread(target=self.worker, daemon=True).start()

    # ---- background work

    def worker(self):
        while not core._stop:
            desc, key, fn = self.jobs.get()
            self.busy, self.current_key = desc, key
            if key:
                self.live[key] = "checking…"
            try:
                with core.locked(self.args.state):
                    state = core.State(self.args.state)
                    try:
                        fn(state)
                    finally:
                        state.save()
                        self.publish(state)
            except KeyboardInterrupt:
                return  # quitting mid-download
            except Exception as e:
                core.log(f"{desc}: error: {e}")
            finally:
                if key:
                    self.live.pop(key, None)
                    self.queued.discard(key)
                self.busy = self.current_key = None

    def publish(self, state):
        with self.snap_lock:
            self.snapshot = copy.deepcopy(state.data)

    def on_progress(self, name, done, total):
        if self.current_key:
            if done is None:
                self.live[self.current_key] = "checking…"
            else:
                pct = f" {done * 100 // total}%" if total else ""
                self.live[self.current_key] = f"↓ {name}{pct}"

    def submit(self, desc, fn, key=None):
        self.jobs.put((desc, key, fn))

    def check(self, rc):
        if rc.key in self.queued:
            return
        self.queued.add(rc.key)
        self.live[rc.key] = "queued"
        notify = self.cfg.get("notify", False)
        self.submit(f"check {self.name(rc)}", lambda st: core.check_repo(rc, st, notify), rc.key)

    def auto_check(self):
        if not self.auto or self.cfg_error:
            return
        interval = self.interval()
        for rc in self.repos:
            if time.time() - (self.st(rc).get("checked") or 0) > interval:
                self.check(rc)

    def interval(self) -> int:
        try:
            return core.parse_interval(self.cfg.get("interval", "6h"))
        except ValueError:
            return 6 * 3600

    # ---- config / state access

    def reload_config(self, force=False):
        try:
            mtime = self.args.config.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self.cfg_mtime and not force:
            return
        self.cfg_mtime = mtime
        try:
            self.cfg, self.repos = core.load_config(self.args.config)
            self.cfg_error = ""
        except (Exception, SystemExit) as e:
            self.cfg_error = f"config error: {e}"
            self.repos = []
        self.sel = min(self.sel, max(0, len(self.repos) - 1))

    def st(self, rc) -> dict:
        with self.snap_lock:
            return self.snapshot.get(rc.key, {})

    def forge(self, rc):
        try:
            return core.make_forge(rc.url, rc.provider, rc.token)
        except (ValueError, KeyError):
            return None

    def name(self, rc) -> str:
        f = self.forge(rc)
        if not f:
            return rc.url
        return f.path if f.host == "github.com" else f"{f.path} @{f.host}"

    def raw_repo(self, url: str) -> dict:
        try:
            for r in self.cfgfile.raw().get("repo", []):
                if r.get("url", "").rstrip("/") == url.rstrip("/"):
                    return dict(r)
        except (OSError, tomllib.TOMLDecodeError):
            pass
        return {}

    def token_for(self, url: str) -> str | None:
        host = urllib.parse.urlsplit(url if "://" in url else "https://github.com").hostname
        return self.cfg.get("tokens", {}).get(host)

    # ---- drawing primitives

    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, s, w - x, attr)
            except curses.error:
                pass  # writing the bottom-right cell raises; harmless

    def bar(self, y, text, attr=None):
        w = self.scr.getmaxyx()[1]
        self.put(y, 0, text.ljust(w), attr if attr is not None else curses.A_REVERSE)

    def flash(self, msg):
        self.msg = (msg, time.time())

    def footer(self, hints):
        h, _ = self.scr.getmaxyx()
        msg, at = self.msg
        if msg and time.time() - at < 6:
            self.put(h - 2, 1, msg, curses.color_pair(CYAN) | curses.A_BOLD)
        self.bar(h - 1, " " + hints)

    def key(self, wait=True):
        self.scr.timeout(-1 if wait else 250)
        try:
            return self.scr.get_wch()
        except curses.error:
            return None
        finally:
            self.scr.timeout(250)

    def prompt(self, label, initial="") -> str | None:
        """One-line editor on the bottom row. Enter accepts, Esc cancels."""
        buf, pos = list(initial), len(initial)
        cursor(1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                x0 = len(label) + 2
                width = max(1, w - x0 - 1)
                start = max(0, pos - width + 1)
                self.put(h - 1, 0, " " * (w - 1))
                self.put(h - 1, 0, label + ":", curses.A_BOLD)
                self.put(h - 1, x0, "".join(buf[start:start + width]))
                self.scr.move(h - 1, min(w - 1, x0 + pos - start))
                k = self.key()
                if k in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf)
                if k == "\x1b":
                    return None
                if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos:
                        pos -= 1
                        del buf[pos]
                elif k == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif k == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif k == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif k in (curses.KEY_HOME, "\x01"):
                    pos = 0
                elif k in (curses.KEY_END, "\x05"):
                    pos = len(buf)
                elif k == "\x15":  # ctrl-u
                    buf, pos = [], 0
                elif isinstance(k, str) and k.isprintable():
                    buf.insert(pos, k)
                    pos += 1
        finally:
            cursor(0)

    def confirm(self, question) -> bool:
        h, w = self.scr.getmaxyx()
        self.bar(h - 1, f" {question} [y/N] ", curses.color_pair(YELLOW) | curses.A_REVERSE | curses.A_BOLD)
        return self.key() in ("y", "Y")

    def status_line(self, text):
        """Show a message immediately (before a blocking operation)."""
        h, _ = self.scr.getmaxyx()
        self.bar(h - 1, " " + text, curses.color_pair(YELLOW) | curses.A_REVERSE)
        self.scr.refresh()

    @staticmethod
    def nav(k, sel, n, page):
        if k in (curses.KEY_UP, "k"):
            sel -= 1
        elif k in (curses.KEY_DOWN, "j"):
            sel += 1
        elif k == curses.KEY_PPAGE:
            sel -= page
        elif k == curses.KEY_NPAGE:
            sel += page
        elif k in (curses.KEY_HOME, "g"):
            sel = 0
        elif k in (curses.KEY_END, "G"):
            sel = n - 1
        return max(0, min(sel, n - 1))

    @staticmethod
    def scroll(sel, top, rows):
        if sel < top:
            return sel
        if sel >= top + rows:
            return sel - rows + 1
        return top

    # ---- main screen

    def draw_main(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        busy = f"working: {self.busy}" if self.busy else "idle"
        nxt = ""
        if not self.auto:
            nxt = " · auto-check off"
        elif self.repos:
            due = min((self.st(rc).get("checked") or 0) for rc in self.repos) + self.interval()
            nxt = f" · auto-check in {max(0, due - time.time()) / 60:.0f}m" if due > time.time() else ""
        self.bar(0, f" releaseclone · {len(self.repos)} repo(s){nxt} · {busy}")
        if self.cfg_error:
            self.put(1, 1, self.cfg_error, curses.color_pair(RED) | curses.A_BOLD)

        log_rows = min(8, h // 3) if self.show_log else 0
        list_top, list_rows = 3, max(1, h - 3 - log_rows - 3)
        tag_w, keep_w, chk_w = 12, 5, 10
        name_w = max(16, min(42, w - (tag_w * 2 + keep_w + chk_w + 30)))
        cols = [("REPO", name_w), ("LATEST", tag_w), ("HAVE", tag_w), ("KEEP", keep_w), ("CHECKED", chk_w)]
        x = 2
        for title, cw in cols:
            self.put(2, x, title, curses.color_pair(CYAN) | curses.A_BOLD)
            x += cw + 1
        self.put(2, x, "STATUS", curses.color_pair(CYAN) | curses.A_BOLD)

        if not self.repos and not self.cfg_error:
            self.put(list_top + 1, 4, "No repos yet. Press  a  to add one.", curses.A_BOLD)
        self.top = self.scroll(self.sel, self.top, list_rows)
        for row, rc in enumerate(self.repos[self.top:self.top + list_rows]):
            i = self.top + row
            y = list_top + row
            st = self.st(rc)
            dl = st.get("downloaded", {})
            have = core.newest_first(st)[0] if dl else "—"
            cells = [self.name(rc), st.get("latest", "—"), have, str(rc.keep or "all"), ago(st.get("checked"))]
            selected = i == self.sel
            base_attr = curses.A_REVERSE if selected else 0
            if selected:
                self.put(y, 0, " " * w, base_attr)
            self.put(y, 0, "›" if selected else " ", base_attr | curses.A_BOLD)
            x = 2
            for (_, cw), cell in zip(cols, cells):
                self.put(y, x, cell[:cw].ljust(cw), base_attr)
                x += cw + 1
            if rc.key in self.live:
                status, attr = self.live[rc.key], curses.color_pair(YELLOW)
            elif st.get("error"):
                status, attr = "✗ " + st["error"].splitlines()[0], curses.color_pair(RED)
            elif dl and not dl[have]["files"]:
                status, attr = "! 0 files matched, edit patterns (p)", curses.color_pair(YELLOW)
            elif dl:
                status, attr = "✓ up to date", curses.color_pair(GREEN)
            elif st.get("checked"):
                status, attr = "nothing downloaded", curses.A_DIM
            else:
                status, attr = "not checked yet", curses.A_DIM
            self.put(y, x, status, base_attr | attr)

        if log_rows:
            y0 = h - 2 - log_rows
            self.put(y0 - 1, 0, "─ log " + "─" * (w - 6), curses.A_DIM)
            for j, line in enumerate(list(self.logs)[-log_rows + 1:]):
                self.put(y0 + j, 1, line, curses.A_DIM)
        self.footer("r check  R all  a add  e edit  p pick files  ⏎ releases  o open  d delete  "
                    "s settings  l log  A auto  q quit")
        self.scr.refresh()

    def main(self):
        cursor(0)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for pair, color in ((GREEN, curses.COLOR_GREEN), (YELLOW, curses.COLOR_YELLOW),
                                (RED, curses.COLOR_RED), (CYAN, curses.COLOR_CYAN)):
                curses.init_pair(pair, color, bg)
        while True:
            self.reload_config()
            self.auto_check()
            self.draw_main()
            k = self.key(wait=False)
            if k is None or k == curses.KEY_RESIZE:
                continue
            rc = self.repos[self.sel] if self.repos else None
            if k in ("q", "Q"):
                if not self.busy or self.confirm("A download/check is running. Quit anyway?"):
                    core._stop = True
                    return
            elif k in ("a", "+"):
                self.add_repo()
            elif k == "s":
                self.settings()
            elif k == "l":
                self.show_log = not self.show_log
            elif k == "L":
                self.log_view()
            elif k == "A":
                self.auto = not self.auto
                self.flash(f"auto-check {'on' if self.auto else 'off'}")
            elif k == "R":
                for r in self.repos:
                    self.check(r)
            elif rc is None:
                continue
            elif k == "r":
                self.check(rc)
            elif k == "e":
                self.edit_repo(rc)
            elif k == "p":
                self.pick_for_repo(rc)
            elif k in ("\n", "\r", curses.KEY_ENTER):
                self.releases_view(rc)
            elif k == "o":
                self.open_dir(rc)
            elif k in ("d", curses.KEY_DC):
                self.delete_repo(rc)
            else:
                self.sel = self.nav(k, self.sel, len(self.repos), 10)

    # ---- actions

    def open_path(self, path: Path):
        if not path.exists():
            self.flash(f"{path} doesn't exist yet")
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            self.flash(f"opened {path}")
        else:
            self.flash(str(path))

    def open_dir(self, rc):
        f = self.forge(rc)
        if f:
            self.open_path(core.repo_dir(rc, f))

    def add_repo(self):
        url = self.prompt("Repo URL (or owner/repo for GitHub)")
        if not url or not url.strip():
            return
        url = url.strip().rstrip("/")
        if "://" not in url:
            url = "https://github.com/" + url
        url = re.sub(r"/(-/)?(releases|tags).*$", "", url)
        raw = {"url": url}
        provider = None
        try:
            core.make_forge(url)
        except ValueError:
            provider = self.prompt("Unknown host. Provider (github/gitlab/gitea/forgejo)")
            if provider not in core.FORGES:
                self.flash("cancelled: unknown provider")
                return
            raw["provider"] = provider
        if any(r.key == url for r in self.repos):
            self.flash("that repo is already configured")
            return
        picked = self.pick_assets(url, provider, None, [], False)
        if picked:
            raw["include"], exclude = picked
            if exclude:
                raw["exclude"] = exclude
        self.repo_form(None, raw)

    def edit_repo(self, rc):
        raw = self.raw_repo(rc.url)
        if not raw:
            self.flash("couldn't find this repo's block in the config")
            return
        self.repo_form(rc, raw)

    def repo_form(self, rc, raw):
        gkeep, gdest = self.cfg.get("keep", 0), self.cfg.get("dest", "~/Downloads/releases")
        fields = [
            Field("URL", "url", raw.get("url", ""), "text", "Repo URL, or owner/repo for GitHub"),
            Field("Include", "include", raw.get("include"), "list",
                  "Comma-separated globs (default: all assets). Press p to pick from the latest release."),
            Field("Exclude", "exclude", raw.get("exclude"), "list", "Comma-separated globs to skip"),
            Field("Keep", "keep", raw.get("keep"), "int",
                  f"Releases to keep before pruning. Empty = global ({gkeep or 'all'}), 0 = keep all"),
            Field("Prereleases", "prereleases", raw.get("prereleases"), "tri", "Also download pre-releases"),
            Field("Provider", "provider", raw.get("provider"), "text",
                  "github / gitlab / gitea / forgejo. Empty = detect from the host"),
            Field("Destination", "dest", raw.get("dest"), "text", f"Download folder. Empty = global ({gdest})"),
            Field("Post command", "post_command", raw.get("post_command"), "text",
                  "Shell command run after each new release ($RC_REPO, $RC_TAG, $RC_DIR)"),
        ]

        def pick(fs):
            vals = {f.key: f.value for f in fs}
            if not vals["url"]:
                return self.flash("set the URL first")
            picked = self.pick_assets(vals["url"], vals["provider"], vals["include"], vals["exclude"] or [],
                                      bool(vals["prereleases"]))
            if picked:
                fs[1].value, fs[2].value = picked[0], picked[1] or None

        title = f"Edit {self.name(rc)}" if rc else "Add repo"
        while True:
            vals = self.form(title, fields, {"p": pick}, "p pick files from latest release")
            if vals is None:
                return
            if not vals["url"]:
                self.flash("URL is required")
                continue
            if vals["provider"] and vals["provider"] not in core.FORGES:
                self.flash(f"provider must be one of: {', '.join(sorted(core.FORGES))}")
                continue
            try:
                core.make_forge(vals["url"], vals["provider"])
            except ValueError as e:
                self.flash(str(e))
                continue
            break

        merged = {**raw, **vals}
        old_url = rc.url if rc else None
        try:
            self.cfgfile.save_repo(old_url, merged)
        except Exception as e:
            self.flash(f"couldn't save config: {e}")
            return
        self.reload_config(force=True)
        new_key = merged["url"].rstrip("/")
        if rc and new_key != rc.key:
            def move(st, old=rc.key, new=new_key):
                if old in st.data:
                    st.data[new] = st.data.pop(old)
            self.submit("rename state", move)
        self.flash("saved")
        new_rc = next((r for r in self.repos if r.key == new_key), None)
        if not new_rc:
            return
        self.sel = self.repos.index(new_rc)
        patterns_changed = rc and (raw.get("include") != vals["include"] or raw.get("exclude") != vals["exclude"])
        st = self.st(new_rc)
        if patterns_changed and st.get("downloaded"):
            newest = core.newest_first(st)[0]
            if self.confirm(f"Patterns changed. Fetch newly-matched files for {newest} now?"):
                self.submit("refetch", lambda s: core.refetch(s.repo(new_rc.key), newest))
                self.check(new_rc)
        elif not rc:
            self.check(new_rc)

    def pick_for_repo(self, rc):
        raw = self.raw_repo(rc.url)
        picked = self.pick_assets(rc.url, rc.provider, raw.get("include"), raw.get("exclude", []), rc.prereleases,
                                  token=rc.token)
        if not picked:
            return
        raw["include"], exclude = picked
        if exclude:
            raw["exclude"] = exclude
        else:
            raw.pop("exclude", None)
        try:
            self.cfgfile.save_repo(rc.url, raw)
        except Exception as e:
            self.flash(f"couldn't save config: {e}")
            return
        self.reload_config(force=True)
        self.flash("patterns saved: " + ", ".join(raw["include"]))
        rc = next((r for r in self.repos if r.key == rc.key), None)  # pick up the new patterns
        if not rc:
            return
        st = self.st(rc)
        if st.get("downloaded"):
            newest = core.newest_first(st)[0]
            if self.confirm(f"Fetch newly-matched files for {newest} now?"):
                self.submit("refetch", lambda s: core.refetch(s.repo(rc.key), newest))
                self.check(rc)

    def delete_repo(self, rc):
        if not self.confirm(f"Remove {self.name(rc)} from the config?"):
            return
        f = self.forge(rc)
        folder = core.repo_dir(rc, f) if f else None
        wipe = bool(folder and folder.exists() and self.confirm(f"Also delete downloaded files in {folder}?"))
        try:
            self.cfgfile.delete_repo(rc.url)
        except Exception as e:
            self.flash(f"couldn't save config: {e}")
            return

        def forget(st, key=rc.key):
            st.data.pop(key, None)
            if wipe:
                shutil.rmtree(folder, ignore_errors=True)
        self.submit(f"remove {self.name(rc)}", forget)
        self.reload_config(force=True)
        self.flash(f"removed {self.name(rc)}" + (" and its files" if wipe else ""))

    def settings(self):
        try:
            raw = self.cfgfile.raw()
        except Exception as e:
            self.flash(f"config error: {e}")
            return
        fields = [
            Field("Download folder", "dest", raw.get("dest"), "text", "Where files go (default ~/Downloads/releases)"),
            Field("Check every", "interval", self.interval_hours(raw.get("interval")), "hours",
                  f"Hours between checks: {', '.join(map(str, CRON_HOURS))}. "
                  "Used by the TUI, `watch` and cron"),
            Field("Background checks", "cron", self.cron_enabled(), "bool",
                  "Check via cron even when the TUI is closed" if Crontab.available()
                  else "Unavailable: the crontab command isn't installed"),
            Field("Keep releases", "keep", raw.get("keep"), "int",
                  "Releases to keep per repo before pruning (0 or empty = keep all). Repos can override."),
            Field("Notifications", "notify", raw.get("notify"), "tri", "Desktop notification for new releases"),
            Field("Prereleases", "prereleases", raw.get("prereleases"), "tri", "Default for all repos"),
            Field("Post command", "post_command", raw.get("post_command"), "text",
                  "Shell command run after each new release ($RC_REPO, $RC_TAG, $RC_DIR)"),
        ]
        old_cron = fields[2].value
        vals = self.form("Settings", fields)
        if vals is None:
            return
        hours, cron = vals.pop("interval"), vals.pop("cron")
        vals["interval"] = f"{hours}h"
        try:
            for key, value in vals.items():
                if raw.get(key) != value:
                    self.cfgfile.set_top(key, value)
        except Exception as e:
            self.flash(f"couldn't save config: {e}")
            return
        self.reload_config(force=True)
        msg = "settings saved"
        if cron or old_cron:
            try:
                Crontab.set(hours if cron else None, self.cron_command())
                msg += f"; cron checks every {hours}h" if cron else "; cron checks removed"
            except Exception as e:
                msg = f"settings saved, but cron wasn't updated: {e}"
        self.flash(msg)

    @staticmethod
    def interval_hours(interval) -> int:
        """Config interval -> the nearest allowed whole-hour value."""
        try:
            secs = core.parse_interval(interval or "6h")
        except ValueError:
            secs = 6 * 3600
        return min(CRON_HOURS, key=lambda h: abs(h * 3600 - secs))

    def cron_enabled(self) -> bool:
        try:
            return Crontab.available() and Crontab.ours() is not None
        except RuntimeError:
            return False

    def cron_command(self) -> str:
        exe = shutil.which("releaseclone") or str(Path(core.__file__).resolve())
        cmd = [exe, "--config", str(self.args.config.resolve()), "--state", str(self.args.state.resolve()), "run"]
        log = self.args.state.resolve().parent / "log"
        line = f"{shlex.join(cmd)} >> {shlex.quote(str(log))} 2>&1"
        return line.replace("%", "\\%")  # cron turns a bare % into a newline

    # ---- generic form

    def form(self, title, fields, extra=None, extra_hint=""):
        sel = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.bar(0, f" {title}")
            lw = max(len(f.label) for f in fields) + 2
            for i, f in enumerate(fields):
                y = 2 + i
                attr = curses.A_REVERSE if i == sel else 0
                self.put(y, 2, f.label.ljust(lw), attr | curses.A_BOLD)
                self.put(y, 2 + lw, " " + f.show(), attr | (curses.A_DIM if f.value in (None, "") else 0))
            self.put(3 + len(fields), 2, fields[sel].help, curses.color_pair(CYAN))
            self.footer(f"⏎ edit  {extra_hint + '  ' if extra_hint else ''}s save  esc cancel")
            self.scr.refresh()
            k = self.key()
            if k in ("\x1b", "q"):
                return None
            if k == "s":
                return {f.key: f.value for f in fields}
            if extra and k in extra:
                extra[k](fields)
            elif k in ("\n", "\r", curses.KEY_ENTER, " "):
                self.edit_field(fields[sel])
            elif k != curses.KEY_RESIZE:
                sel = self.nav(k, sel, len(fields), 5)

    def edit_field(self, f: Field):
        if f.kind == "tri":
            f.value = {None: True, True: False, False: None}[f.value]
            return
        if f.kind == "bool":
            f.value = not f.value
            return
        cur = ", ".join(f.value) if f.kind == "list" and f.value else ("" if f.value is None else str(f.value))
        s = self.prompt(f.label, cur)
        if s is None:
            return
        s = s.strip()
        if f.kind == "list":
            f.value = [p.strip() for p in s.split(",") if p.strip()] or None
        elif f.kind == "int":
            if s and not s.isdigit():
                self.flash("must be a whole number")
                return
            f.value = int(s) if s else None
        elif f.kind == "hours":
            if not s.isdigit() or int(s) not in CRON_HOURS:
                self.flash(f"hours must be one of {', '.join(map(str, CRON_HOURS))} (they divide the day evenly)")
                return
            f.value = int(s)
        else:
            f.value = s or None

    # ---- asset picker

    def pick_assets(self, url, provider, include, exclude, prereleases, token=None):
        """Let the user tick assets of the latest release; returns (include, exclude) or None."""
        self.status_line("fetching latest release…")
        try:
            forge = core.make_forge(url, provider, token or self.token_for(url))
            releases = forge.parse(core.api_get(forge, forge.auth_headers())[0])
        except Exception as e:
            self.flash(f"couldn't fetch releases: {e}")
            return None
        releases = [r for r in releases if prereleases or not r.prerelease]
        if not releases:
            self.flash("no releases found")
            return None
        rel = max(releases, key=lambda r: r.published)
        assets = rel.assets
        if not assets:
            self.flash(f"{rel.tag} has no downloadable assets")
            return None
        checked = {a.name for a in core.select_assets(rel, include, exclude)} if include is not None else set()
        filt, sel, top = "", 0, 0
        while True:
            shown = [a for a in assets if filt.lower() in a.name.lower()]
            sel = min(sel, max(0, len(shown) - 1))
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.bar(0, f" Pick files · {forge.path} {rel.tag} · {len(assets)} assets"
                        + (f" · filter: {filt}" if filt else ""))
            rows = max(1, h - 6)
            top = self.scroll(sel, top, rows)
            for row, a in enumerate(shown[top:top + rows]):
                i = top + row
                on = a.name in checked
                attr = (curses.A_REVERSE if i == sel else 0) | (curses.color_pair(GREEN) if on else 0)
                size = human(a.size).rjust(9) if a.size else " " * 9
                self.put(2 + row, 1, f"[{'x' if on else ' '}] {size}  {a.name}".ljust(w - 2), attr)
            total = sum(a.size or 0 for a in assets if a.name in checked)
            pats = list(dict.fromkeys(generalize(n, rel.tag) for n in sorted(checked)))
            self.put(h - 3, 1, f"{len(checked)} selected · {human(total)} per release", curses.A_BOLD)
            self.put(h - 2, 1, "patterns: " + (", ".join(pats) or "—"), curses.color_pair(CYAN))
            self.bar(h - 1, " space toggle  a all  n none  / filter  ⏎ accept  esc cancel")
            self.scr.refresh()
            k = self.key()
            if k == "\x1b":
                return None
            if k == " " and shown:
                checked ^= {shown[sel].name}
                sel = min(sel + 1, len(shown) - 1)
            elif k == "a":
                checked |= {a.name for a in shown}
            elif k == "n":
                checked -= {a.name for a in shown}
            elif k == "/":
                f = self.prompt("Filter", filt)
                if f is not None:
                    filt, sel, top = f, 0, 0
            elif k in ("\n", "\r", curses.KEY_ENTER):
                if not checked and not self.confirm("Nothing selected, so nothing will be downloaded. OK?"):
                    continue
                if checked == {a.name for a in assets}:
                    pats = ["*"]
                matched = {a.name for a in core.select_assets(rel, pats, exclude)}
                if not checked <= matched:  # an exclude pattern hides a chosen file
                    exclude = []
                    matched = {a.name for a in core.select_assets(rel, pats, exclude)}
                extra = matched - checked
                if extra:
                    self.flash(f"note: patterns also match {len(extra)} unticked file(s): "
                               + ", ".join(sorted(extra)[:3]))
                return pats, exclude
            elif k != curses.KEY_RESIZE:
                sel = self.nav(k, sel, len(shown), rows)

    # ---- releases view

    def releases_view(self, rc):
        f = self.forge(rc)
        if not f:
            self.flash("can't resolve this repo's URL")
            return
        base = core.repo_dir(rc, f)
        sel = 0
        while True:
            st = self.st(rc)
            tags = core.newest_first(st) if st.get("downloaded") else []
            sel = min(sel, max(0, len(tags) - 1))
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.bar(0, f" {self.name(rc)} · downloaded releases · keep {rc.keep or 'all'} · {base}")
            if not tags:
                self.put(2, 2, "Nothing downloaded yet." + (f"  ({self.live[rc.key]})" if rc.key in self.live else ""))
            list_rows = max(1, min(len(tags), (h - 6) // 2))
            for i, tag in enumerate(tags[:list_rows]):
                info = st["downloaded"][tag]
                d = base / core.safe_name(tag)
                on_disk = human(dir_size(d)) if d.is_dir() else "missing"
                line = f"{tag:<20} {info['published'][:10]}  {len(info['files']):>3} file(s)  {on_disk:>10}"
                if i == 0:
                    line += "   ← latest"
                self.put(2 + i, 2, line.ljust(w - 4), curses.A_REVERSE if i == sel else 0)
            if tags:
                y = 3 + list_rows
                self.put(y, 2, f"Files in {tags[sel]}:", curses.color_pair(CYAN) | curses.A_BOLD)
                d = base / core.safe_name(tags[sel])
                for j, name in enumerate(st["downloaded"][tags[sel]]["files"][:max(0, h - y - 4)]):
                    p = d / name
                    size = human(p.stat().st_size) if p.exists() else "missing"
                    self.put(y + 1 + j, 4, f"{size:>10}  {name}")
            self.footer("d delete  g download again  o open folder  r check now  esc back")
            self.scr.refresh()
            k = self.key(wait=False)
            if k is None or k == curses.KEY_RESIZE:
                continue
            if k in ("\x1b", "q", curses.KEY_LEFT):
                return
            if k == "r":
                self.check(rc)
            elif k == "o":
                self.open_path(base / core.safe_name(tags[sel]) if tags else base)
            elif k == "d" and tags:
                tag = tags[sel]
                if self.confirm(f"Delete {tag} from disk? (it won't be downloaded again unless you press g)"):
                    self.submit(f"delete {tag}", lambda s, t=tag: core.delete_release(base, s.repo(rc.key), t))
                    self.flash(f"deleted {tag}")
            elif k == "g" and tags:
                tag = tags[sel]
                self.submit(f"refetch {tag}", lambda s, t=tag: core.refetch(s.repo(rc.key), t))
                self.check(rc)
                self.flash(f"re-downloading {tag}")
            else:
                sel = self.nav(k, sel, len(tags), 5)

    # ---- log view

    def log_view(self):
        offset = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            lines = list(self.logs)
            rows = h - 2
            offset = max(0, min(offset, len(lines) - rows))
            end = len(lines) - offset
            self.bar(0, f" Log · {len(lines)} lines" + (f" · scrolled {offset}" if offset else ""))
            for i, line in enumerate(lines[max(0, end - rows):end]):
                self.put(1 + i, 1, line)
            self.bar(h - 1, " ↑↓ PgUp PgDn scroll  esc back")
            self.scr.refresh()
            k = self.key(wait=False)
            if k in ("\x1b", "q", "L"):
                return
            if k in (curses.KEY_UP, "k"):
                offset += 1
            elif k in (curses.KEY_DOWN, "j"):
                offset -= 1
            elif k == curses.KEY_PPAGE:
                offset += rows
            elif k == curses.KEY_NPAGE:
                offset -= rows


def run(core_module, args):
    global core
    core = core_module
    if not args.config.exists():
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(NEW_CONFIG)
    curses.set_escdelay(25)

    def start(scr):
        App(scr, args).main()

    try:
        curses.wrapper(start)
    finally:
        core._log_sink = core._progress_sink = None
