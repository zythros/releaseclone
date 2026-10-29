#!/usr/bin/env python3
"""releaseclone - watch software forges for new releases and download their assets.

Supports GitHub, GitLab (gitlab.com or self-hosted) and Gitea/Forgejo (e.g. Codeberg).
Pure standard library; needs Python 3.11+ (for tomllib).

    releaseclone.py run              # one pass over every configured repo
    releaseclone.py watch            # run forever, checking every `interval`
    releaseclone.py list URL         # show a repo's latest release and its assets
    releaseclone.py status           # show what has been downloaded so far
"""

from __future__ import annotations

import argparse
import contextlib
import email.utils
import fnmatch
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # not on Windows
    fcntl = None

VERSION = "1.0"
USER_AGENT = f"releaseclone/{VERSION}"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
DEFAULT_CONFIG = CONFIG_HOME / "releaseclone" / "config.toml"
DEFAULT_STATE = DATA_HOME / "releaseclone" / "state.json"
FETCH_COUNT = 10  # releases fetched per check

_stop = False
_log_sink = None  # the TUI sets these to capture output instead of printing
_progress_sink = None


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if _log_sink:
        _log_sink(line)
    else:
        print(line, flush=True)


def progress(name: str, done: int | None, total: int | None) -> None:
    """Report download progress; done=None means the download finished."""
    if _progress_sink:
        _progress_sink(name, done, total)
    elif sys.stdout.isatty():
        if done is None:
            print("\r\033[K", end="")
        else:
            pct = f"{done * 100 // total:3d}%" if total else ""
            print(f"\r    {name}  {done / 1e6:.1f} MB {pct}", end="", flush=True)


@contextlib.contextmanager
def locked(state_path: Path):
    """Serialise state access between concurrent instances (e.g. `watch` and `tui`)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path.with_suffix(".lock"), "w") as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


# --------------------------------------------------------------------------- HTTP


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop credentials when a redirect leaves the original host (e.g. to a CDN)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            for h in ("Authorization", "Private-token"):
                new.remove_header(h)
                new.unredirected_hdrs.pop(h, None)
        return new


_opener = urllib.request.build_opener(_StripAuthOnRedirect)


def http_open(url: str, headers: dict[str, str] | None = None, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return _opener.open(req, timeout=timeout)


# --------------------------------------------------------------------------- forges


@dataclass
class Asset:
    name: str
    url: str
    size: int | None = None
    sha256: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Release:
    tag: str
    name: str
    published: str
    prerelease: bool
    assets: list[Asset]


class Forge:
    def __init__(self, host: str, path: str, token: str | None):
        self.host, self.path, self.token = host, path, token

    def api_url(self) -> str: ...
    def auth_headers(self) -> dict[str, str]: ...
    def parse(self, data) -> list[Release]: ...


class GitHub(Forge):
    def api_url(self):
        api = "https://api.github.com" if self.host == "github.com" else f"https://{self.host}/api/v3"
        return f"{api}/repos/{self.path}/releases?per_page={FETCH_COUNT}"

    def auth_headers(self):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def parse(self, data):
        out = []
        for r in data:
            if r.get("draft"):
                continue
            assets = []
            for a in r.get("assets", []):
                digest = a.get("digest") or ""
                sha = digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
                if self.token:  # API URL works for private repos too
                    asset = Asset(a["name"], a["url"], a.get("size"), sha,
                                  {"Accept": "application/octet-stream", "Authorization": f"Bearer {self.token}"})
                else:
                    asset = Asset(a["name"], a["browser_download_url"], a.get("size"), sha)
                assets.append(asset)
            out.append(Release(r["tag_name"], r.get("name") or r["tag_name"], r.get("published_at") or "",
                               bool(r.get("prerelease")), assets))
        return out


class GitLab(Forge):
    def api_url(self):
        pid = urllib.parse.quote(self.path, safe="")
        return f"https://{self.host}/api/v4/projects/{pid}/releases?per_page={FETCH_COUNT}"

    def auth_headers(self):
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    def parse(self, data):
        out = []
        for r in data:
            links = r.get("assets", {}).get("links", [])
            assets = [Asset(l["name"], l.get("direct_asset_url") or l["url"], headers=self.auth_headers())
                      for l in links]
            out.append(Release(r["tag_name"], r.get("name") or r["tag_name"], r.get("released_at") or "",
                               bool(r.get("upcoming_release")), assets))
        return out


class Gitea(Forge):
    def api_url(self):
        return f"https://{self.host}/api/v1/repos/{self.path}/releases?limit={FETCH_COUNT}"

    def auth_headers(self):
        return {"Authorization": f"token {self.token}"} if self.token else {}

    def parse(self, data):
        out = []
        for r in data:
            if r.get("draft"):
                continue
            assets = [Asset(a["name"], a["browser_download_url"], a.get("size"), headers=self.auth_headers())
                      for a in r.get("assets", [])]
            out.append(Release(r["tag_name"], r.get("name") or r["tag_name"], r.get("published_at") or "",
                               bool(r.get("prerelease")), assets))
        return out


FORGES = {"github": GitHub, "gitlab": GitLab, "gitea": Gitea, "forgejo": Gitea}
TOKEN_ENV = {GitHub: "GITHUB_TOKEN", GitLab: "GITLAB_TOKEN", Gitea: "GITEA_TOKEN"}


def make_forge(url: str, provider: str | None = None, token: str | None = None) -> Forge:
    if "://" not in url:
        url = "https://github.com/" + url  # "owner/repo" shorthand
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    path = parts.path.strip("/").removesuffix(".git")
    path = re.sub(r"/(-/)?(releases|tags).*$", "", path)  # accept .../releases URLs
    if provider:
        cls = FORGES[provider.lower()]
    elif host == "github.com":
        cls = GitHub
    elif "gitlab" in host:
        cls = GitLab
    elif host in ("codeberg.org",) or "gitea" in host or "forgejo" in host:
        cls = Gitea
    else:
        raise ValueError(f"can't tell which forge {host} runs; set provider = github|gitlab|gitea")
    return cls(host, path, token or os.environ.get(TOKEN_ENV[cls]))


# --------------------------------------------------------------------------- config / state


@dataclass
class RepoConfig:
    url: str
    include: list[str]
    exclude: list[str]
    prereleases: bool
    keep: int
    dest: Path
    provider: str | None
    token: str | None
    post_command: str | None

    @property
    def key(self) -> str:
        return self.url.rstrip("/")


def parse_interval(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(v))
    if not m:
        raise ValueError(f"bad interval {v!r} (use e.g. 30m, 6h, 1d)")
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def load_config(path: Path, keep: int | None = None) -> tuple[dict, list[RepoConfig]]:
    if not path.exists():
        sys.exit(f"No config at {path}. Copy config.example.toml there, or pass --config.")
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    dest = Path(cfg.get("dest", "~/Downloads/releases")).expanduser()
    tokens = cfg.get("tokens", {})
    repos = []
    for r in cfg.get("repo", []):
        forge_host = urllib.parse.urlsplit(r["url"] if "://" in r["url"] else "https://github.com").hostname
        repos.append(RepoConfig(
            url=r["url"],
            include=r.get("include", ["*"]),
            exclude=r.get("exclude", []),
            prereleases=r.get("prereleases", cfg.get("prereleases", False)),
            keep=keep if keep is not None else r.get("keep", cfg.get("keep", 0)),
            dest=Path(r["dest"]).expanduser() if "dest" in r else dest,
            provider=r.get("provider"),
            token=r.get("token") or tokens.get(forge_host),
            post_command=r.get("post_command", cfg.get("post_command")),
        ))
    return cfg, repos


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def repo(self, key: str) -> dict:
        return self.data.setdefault(key, {"seen": [], "downloaded": {}})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


# --------------------------------------------------------------------------- core


def select_assets(rel: Release, include: list[str], exclude: list[str]) -> list[Asset]:
    return [a for a in rel.assets
            if any(fnmatch.fnmatch(a.name, p) for p in include)
            and not any(fnmatch.fnmatch(a.name, p) for p in exclude)]


def safe_name(name: str) -> str:
    return re.sub(r"[/\\\0]", "_", name).lstrip(".") or "_"


def download(asset: Asset, target: Path) -> None:
    if target.exists() and asset.size and target.stat().st_size == asset.size:
        log(f"    already have {target.name}")
        return
    part = target.with_name(target.name + ".part")
    h = hashlib.sha256()
    done = 0
    with http_open(asset.url, asset.headers, timeout=120) as resp, part.open("wb") as out:
        total = asset.size or int(resp.headers.get("Content-Length") or 0)
        last = 0.0
        while chunk := resp.read(1 << 20):
            if _stop:
                raise KeyboardInterrupt
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if time.monotonic() - last > 0.5:
                progress(target.name, done, total)
                last = time.monotonic()
    progress(target.name, None, None)
    if asset.sha256 and h.hexdigest() != asset.sha256.lower():
        part.unlink()
        raise RuntimeError(f"checksum mismatch for {asset.name}")
    if total and done != total:
        part.unlink()
        raise RuntimeError(f"incomplete download for {asset.name} ({done}/{total} bytes)")
    part.replace(target)
    if target.suffix == ".AppImage":
        target.chmod(target.stat().st_mode | 0o111)
    log(f"    got {target.name} ({done / 1e6:.1f} MB{', sha256 ok' if asset.sha256 else ''})")


def verify_sums_files(folder: Path) -> None:
    """If a SHA256SUMS-style file was downloaded, check the other files against it."""
    for sums in folder.iterdir():
        if not re.search(r"sha256sums|checksums", sums.name, re.I) or sums.stat().st_size > 1 << 20:
            continue
        for line in sums.read_text(errors="replace").splitlines():
            m = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
            if not m:
                continue
            f = folder / safe_name(Path(m[2]).name)
            if f.is_file() and f != sums:
                if sha256_file(f) != m[1].lower():
                    raise RuntimeError(f"{f.name} does not match {sums.name}")
                log(f"    {f.name} matches {sums.name}")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def repo_dir(rc: RepoConfig, forge: Forge) -> Path:
    return rc.dest / safe_name(forge.path.replace("/", "_"))


def newest_first(st: dict) -> list[str]:
    return sorted(st["downloaded"], key=lambda t: st["downloaded"][t]["published"], reverse=True)


def update_latest(base: Path, st: dict) -> None:
    """Point <base>/latest at the newest downloaded release."""
    latest = base / "latest"
    if not base.is_dir() or (latest.exists() and not latest.is_symlink()):
        return
    latest.unlink(missing_ok=True)
    tags = [t for t in newest_first(st) if (base / safe_name(t)).is_dir()]
    if tags:
        latest.symlink_to(safe_name(tags[0]))


def delete_release(base: Path, st: dict, tag: str) -> None:
    """Remove a downloaded release. It stays 'seen', so it won't be downloaded again."""
    d = base / safe_name(tag)
    if d.is_dir():
        shutil.rmtree(d)
    st["downloaded"].pop(tag, None)
    update_latest(base, st)


def refetch(st: dict, tag: str) -> None:
    """Mark a release as unseen so the next check downloads it (again)."""
    if tag in st["seen"]:
        st["seen"].remove(tag)
    st.pop("etag", None)


def prune(base: Path, keep: int, st: dict) -> None:
    if keep > 0:
        for tag in newest_first(st)[keep:]:
            delete_release(base, st, tag)
            log(f"  pruned old release {tag}")
    update_latest(base, st)


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "releaseclone", title, body], check=False)


class RateLimited(RuntimeError):
    pass


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def rate_limit_reset(code: int, headers) -> float | None:
    """If a response says we're (about to be) rate limited, return when we may ask again."""
    retry = headers.get("Retry-After")
    remaining = headers.get("X-RateLimit-Remaining") or headers.get("RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset") or headers.get("RateLimit-Reset")
    # A 403 is only a rate limit if the headers say so; otherwise it's e.g. a permissions problem.
    if not (code == 429 or (code in (200, 403) and remaining == "0") or (code == 403 and retry)):
        return None
    now = time.time()
    if retry:
        if retry.isdigit():
            return now + int(retry)
        with contextlib.suppress(TypeError, ValueError):
            return email.utils.parsedate_to_datetime(retry).timestamp()
    if reset and reset.isdigit():
        n = int(reset)
        return n if n > 1e9 else now + n  # epoch seconds (GitHub, GitLab) or a delay
    return now + 60  # limited, but no hint as to how long: GitHub's advice is wait a minute


def api_get(forge: Forge, headers: dict[str, str], limits: dict | None = None):
    """GET the forge's releases list -> (data, etag), honouring and recording rate limits.

    `limits` maps host -> reset time; hosts still limited aren't contacted at all.
    A 304 Not Modified is raised as HTTPError for the caller to handle.
    """
    if limits is not None and limits.get(forge.host, 0) > time.time():
        raise RateLimited(f"{forge.host} rate limit: not checking until {hhmm(limits[forge.host])}")
    try:
        with http_open(forge.api_url(), headers) as resp:
            data = json.load(resp)
            etag = resp.headers.get("ETag")
            until = rate_limit_reset(resp.status, resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise
        until = rate_limit_reset(e.code, e.headers)
        if until:
            if limits is not None:
                limits[forge.host] = until
            hint = " (set GITHUB_TOKEN for a higher limit)" if isinstance(forge, GitHub) and not forge.token else ""
            raise RateLimited(f"{forge.host} rate limit hit; waiting until {hhmm(until)}{hint}") from None
        detail = e.read(300).decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {forge.host}: {detail}") from None
    if until and limits is not None:  # that was our last allowed request; don't attempt more
        limits[forge.host] = until
        log(f"{forge.host} rate limit used up; pausing checks until {hhmm(until)}")
    return data, etag


def check_repo(rc: RepoConfig, state: State, do_notify: bool) -> None:
    forge = make_forge(rc.url, rc.provider, rc.token)
    st = state.repo(rc.key)
    st["checked"] = time.time()
    limits = state.data.setdefault("_hosts", {})  # host -> time its rate limit resets
    for host in [h for h, until in limits.items() if until <= time.time()]:
        del limits[host]
    try:
        _check_repo(rc, forge, st, do_notify, limits)
        st["error"] = None
    except Exception as e:
        st["error"] = str(e)
        raise
    finally:
        # Prune on every check, so lowering `keep` takes effect without waiting for a new release.
        prune(repo_dir(rc, forge), rc.keep, st)


def _check_repo(rc: RepoConfig, forge: Forge, st: dict, do_notify: bool, limits: dict) -> None:
    headers = forge.auth_headers()
    if st.get("etag"):
        headers["If-None-Match"] = st["etag"]
    try:
        data, etag = api_get(forge, headers, limits)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            log(f"{rc.key}: no changes")
            return
        raise

    releases = [r for r in forge.parse(data) if rc.prereleases or not r.prerelease]
    releases.sort(key=lambda r: r.published)
    if releases:
        st["latest"] = releases[-1].tag
    first_run = not st["seen"]
    if first_run and releases:
        # Don't backfill history: only grab the newest release the first time.
        st["seen"] = [r.tag for r in releases[:-1]]
    new = [r for r in releases if r.tag not in st["seen"]]
    if not new:
        log(f"{rc.key}: up to date ({releases[-1].tag if releases else 'no releases'})")
        st["etag"] = etag
        return

    base = repo_dir(rc, forge)
    ok = True
    for rel in new:
        assets = select_assets(rel, rc.include, rc.exclude)
        log(f"{rc.key}: new release {rel.tag} — {len(assets)} of {len(rel.assets)} assets match")
        folder = base / safe_name(rel.tag)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            for a in assets:
                download(a, folder / safe_name(a.name))
            verify_sums_files(folder)
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            log(f"  ! {rel.tag} failed: {e} (will retry next check)")
            ok = False
            continue
        st["seen"].append(rel.tag)
        st["downloaded"][rel.tag] = {"published": rel.published, "files": [safe_name(a.name) for a in assets],
                                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if do_notify:
            notify(f"New release: {forge.path} {rel.tag}", f"{len(assets)} file(s) in {folder}")
        if rc.post_command:
            env = {**os.environ, "RC_REPO": forge.path, "RC_TAG": rel.tag, "RC_DIR": str(folder)}
            subprocess.run(rc.post_command, shell=True, env=env, check=False)
    st["seen"] = st["seen"][-200:]
    if ok:
        st["etag"] = etag  # only cache the ETag once everything succeeded, so failures get retried


def run_once(cfg: dict, repos: list[RepoConfig], state_path: Path) -> None:
    for rc in repos:
        if _stop:
            break
        with locked(state_path):
            state = State(state_path)  # re-read: another instance may have changed it
            try:
                check_repo(rc, state, cfg.get("notify", False))
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep going with the other repos
                log(f"{rc.key}: error: {e}")
            finally:
                state.save()


# --------------------------------------------------------------------------- commands


def cmd_run(args) -> None:
    cfg, repos = load_config(args.config, args.keep)
    run_once(cfg, repos, args.state)


def cmd_watch(args) -> None:
    def stop(*_):
        global _stop
        _stop = True
        log("stopping…")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not _stop:
        cfg, repos = load_config(args.config, args.keep)  # re-read so edits apply without a restart
        interval = parse_interval(args.interval or cfg.get("interval", "6h"))
        try:
            run_once(cfg, repos, args.state)
        except KeyboardInterrupt:
            break
        wait = interval + random.uniform(0, min(interval * 0.1, 300))
        log(f"next check in {wait / 60:.0f} min")
        end = time.monotonic() + wait
        while not _stop and time.monotonic() < end:
            time.sleep(1)


def cmd_list(args) -> None:
    forge = make_forge(args.url, args.provider)
    try:
        releases = forge.parse(api_get(forge, forge.auth_headers())[0])
    except RuntimeError as e:
        sys.exit(str(e))
    if not args.prereleases:
        releases = [r for r in releases if not r.prerelease]
    if not releases:
        print("no releases found")
        return
    rel = max(releases, key=lambda r: r.published)
    print(f"{forge.path}  latest: {rel.tag}  ({rel.published})\n")
    include = args.include or ["*"]
    picked = {a.name for a in select_assets(rel, include, args.exclude or [])}
    for a in rel.assets:
        mark = "*" if a.name in picked else " "
        size = f"{a.size / 1e6:8.1f} MB" if a.size else " " * 11
        print(f" {mark} {size}  {a.name}")
    if args.include or args.exclude:
        total = sum(a.size or 0 for a in rel.assets if a.name in picked)
        print(f"\n{len(picked)} asset(s) selected, {total / 1e6:.1f} MB")


def cmd_status(args) -> None:
    state = State(args.state)
    if not state.data:
        print("nothing downloaded yet")
    for key, st in state.data.items():
        if key.startswith("_"):  # internal bookkeeping, e.g. rate limits
            continue
        print(key)
        for tag, info in sorted(st["downloaded"].items(), key=lambda kv: kv[1]["published"], reverse=True):
            print(f"  {tag:20} {info['at']}  {len(info['files'])} file(s)")


def cmd_tui(args) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import releaseclone_tui

    releaseclone_tui.run(sys.modules[__name__], args)


def main() -> None:
    p = argparse.ArgumentParser(description="Watch forges for new releases and download them.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"default: {DEFAULT_CONFIG}")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE, help=f"default: {DEFAULT_STATE}")
    sub = p.add_subparsers(dest="cmd", required=True)
    keep_help = "releases to keep per repo, overriding the config (0 = keep all)"
    r = sub.add_parser("run", help="check every repo once")
    r.add_argument("--keep", type=int, metavar="N", help=keep_help)
    r.set_defaults(func=cmd_run)
    w = sub.add_parser("watch", help="check repeatedly")
    w.add_argument("--interval", help="override config interval, e.g. 30m, 6h")
    w.add_argument("--keep", type=int, metavar="N", help=keep_help)
    w.set_defaults(func=cmd_watch)
    l = sub.add_parser("list", help="show a repo's latest release assets (to test patterns)")
    l.add_argument("url")
    l.add_argument("-i", "--include", action="append", help="glob; repeatable")
    l.add_argument("-x", "--exclude", action="append", help="glob; repeatable")
    l.add_argument("--prereleases", action="store_true")
    l.add_argument("--provider", choices=sorted(FORGES))
    l.set_defaults(func=cmd_list)
    sub.add_parser("status", help="show downloaded releases").set_defaults(func=cmd_status)
    sub.add_parser("tui", help="interactive terminal UI").set_defaults(func=cmd_tui)
    args = p.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
