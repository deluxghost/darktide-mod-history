from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import tomllib

from .captions import build_captions, find_bot_names, load_updates
from .avatars import write_avatar, write_identicon

ROOT = Path(__file__).resolve().parents[2]
CODE_SEARCH_MAX_BYTES = 384 * 1024
CODE_SEARCH_MAX_RESULTS = 1000
SEARCH_REQUEST_INTERVAL = 12.0
_next_search_at = 0.0


def load_config() -> dict:
    with (ROOT / "config.toml").open("rb") as stream:
        return tomllib.load(stream)


def output_paths(cfg: dict) -> dict[str, Path]:
    root = ROOT / cfg["output_dir"]
    return {
        "root": root,
        "repos": root / "repos",
        "avatars": root / "avatars",
        "repositories": root / "repositories.json",
        "metadata": root / "metadata.json",
        "contributors": root / "contributors.json",
        "history": root / "history.log",
        "render_log": root / "render.log",
        "captions": root / "captions.txt",
        "ppm": root / "gource.ppm",
        "video": root / "darktide-mod-history.mp4",
    }


def github_json(url: str) -> object:
    global _next_search_at
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for GitHub Code Search")
    if url.startswith("https://api.github.com/search/"):
        time.sleep(max(0.0, _next_search_at - time.monotonic()))
        _next_search_at = time.monotonic() + SEARCH_REQUEST_INTERVAL
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "darktide-mod-history",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def run(command: list[str], cwd: Path | None = None, capture: bool = False, *, env: dict | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, check=True, text=True, encoding="utf-8", stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ""


def search_code(cfg: dict, size_query: str, page: int) -> dict:
    query = urllib.parse.urlencode({
        "q": f"{size_query} {cfg['search_query']}",
        "page": page,
        "per_page": cfg["per_page"],
    })
    result = github_json(f"https://api.github.com/search/code?{query}")
    if result["incomplete_results"]:
        raise RuntimeError(f"GitHub returned incomplete search results for {size_query}, page {page}")
    return result


def discover(cfg: dict, out: dict[str, Path]) -> None:
    records: dict[str, dict] = {}
    excluded_names: set[str] = set()
    start = dt.datetime.combine(dt.date.fromisoformat(cfg["search_start_date"]), dt.time(), dt.UTC)
    if not 1 <= cfg["per_page"] <= 100 or not 1 <= cfg["search_partition_limit"] <= 1000:
        raise ValueError("per_page must be 1..100 and search_partition_limit must be 1..1000")
    # Mod manifests cluster below 1 KiB; cover the remaining search space too.
    pending = [(1024, CODE_SEARCH_MAX_BYTES), (0, 1023)]
    while pending:
        range_start, range_end = pending.pop()
        size_query = f"size:{range_start}..{range_end}"
        first_result = search_code(cfg, size_query, 1)
        total_count = first_result["total_count"]
        print(f"{size_query}: {total_count} code results", flush=True)
        needs_split = total_count > cfg["search_partition_limit"]
        partition_items = []
        if not needs_split:
            max_pages = (CODE_SEARCH_MAX_RESULTS + cfg["per_page"] - 1) // cfg["per_page"]
            for page in range(1, max_pages + 1):
                result = first_result if page == 1 else search_code(cfg, size_query, page)
                partition_items.extend(result["items"])
                if len(result["items"]) < cfg["per_page"]:
                    break
            else:
                # total_count can underestimate results; never accept a saturated page range.
                needs_split = True
        if needs_split:
            if range_start >= range_end:
                raise RuntimeError(f"cannot partition search results for {size_query}")
            midpoint = range_start + (range_end - range_start) // 2
            pending.extend(((midpoint + 1, range_end), (range_start, midpoint)))
            continue
        for item in partition_items:
            repo = item["repository"]
            reason = repository_exclusion(repo["full_name"], cfg)
            if reason:
                if repo["full_name"] not in excluded_names:
                    excluded_names.add(repo["full_name"])
                    print(f"excluded {repo['full_name']}: {reason}", flush=True)
                continue
            records[repo["full_name"]] = repo
    accepted = []
    for full_name in sorted(records):
        repo = github_json(f"https://api.github.com/repos/{full_name}")
        if dt.datetime.fromisoformat(repo["created_at"]) >= start:
            accepted.append(repo)
        else:
            print(f"excluded {full_name}: created_at={repo['created_at']}", flush=True)
    out["root"].mkdir(parents=True, exist_ok=True)
    out["repositories"].write_text(json.dumps(accepted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"excluded {len(excluded_names)} repositories by name")
    print(f"discovered {len(accepted)} repositories created since {start.date()} from {len(records)} candidates after name filtering")


def repository_exclusion(full_name: str, cfg: dict) -> str | None:
    owner, name = full_name.split("/", 1)
    if "vermintide" in name.casefold():
        return "repository name contains vermintide"
    for excluded in cfg["excluded_owners"]:
        if owner.casefold() == excluded.casefold():
            return "owner is in excluded_owners"
    for excluded in cfg["excluded_repositories"]:
        if full_name.casefold() == excluded.casefold():
            return "repository is in excluded_repositories"
    return None


def fetch(cfg: dict, out: dict[str, Path]) -> None:
    bindings = load_author_bindings(cfg["author_bindings"])
    candidates = json.loads(out["repositories"].read_text(encoding="utf-8"))
    out["repos"].mkdir(parents=True, exist_ok=True)
    out["avatars"].mkdir(parents=True, exist_ok=True)
    accepted = []
    for repo in candidates:
        reason = repository_exclusion(repo["full_name"], cfg)
        if reason:
            print(f"excluded {repo['full_name']}: {reason}", flush=True)
            continue
        local = out["repos"] / repo["full_name"].replace("/", "__")
        if local.exists():
            run(["git", "fetch", "origin", "--prune"], local)
        else:
            run(["git", "clone", "--no-checkout", repo["clone_url"], str(local)])
        ref = f"refs/remotes/origin/{repo['default_branch']}"
        revision = run(["git", "rev-parse", "--verify", ref], local, True).strip()
        accepted.append({
            "full_name": repo["full_name"],
            "name": repo["name"],
            "owner": repo["owner"]["login"],
            "description": repo.get("description"),
            "html_url": repo["html_url"],
            "default_branch": repo["default_branch"],
            "revision": revision,
            "created_at": repo["created_at"],
            "updated_at": repo["updated_at"],
            "local_path": str(local.relative_to(out["root"])),
        })
    fetch_contributors(accepted, out, bindings)
    out["metadata"].write_text(json.dumps(accepted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"fetched {len(accepted)} repositories")


def contributor_key(name: str, email: str) -> str:
    identity = f"{name}\0{email.strip().casefold()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def github_contributor(account: dict) -> tuple[str, dict]:
    return f"github:{account['id']}", {
        "name": account["login"],
        "avatar_url": account["avatar_url"],
        "source": "github",
        "github_type": account["type"],
    }


def load_author_bindings(entries: list[dict]) -> dict[str, tuple[str, dict]]:
    bindings = {}
    accounts = {}
    for entry in entries:
        key = contributor_key(entry["name"], entry["email"])
        if key in bindings:
            raise ValueError(f"Duplicate author binding for {entry['name']!r}")
        login = entry["github_login"]
        if not login or login != login.strip():
            raise ValueError("Author binding requires a non-empty GitHub login without surrounding whitespace")
        if login.casefold() not in accounts:
            account = github_json(f"https://api.github.com/users/{urllib.parse.quote(login, safe='')}")
            accounts[login.casefold()] = account
        bindings[key] = github_contributor(accounts[login.casefold()])
    return bindings


def resolve_contributor(repo: dict, commit: str, name: str, key: str) -> tuple[str, dict]:
    result = github_json(f"https://api.github.com/repos/{repo['full_name']}/commits/{commit}")
    account = result["author"]
    if account is not None:
        return github_contributor(account)
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not label:
        raise ValueError(f"Commit {repo['full_name']}@{commit} has no usable author name")
    # Unlinked identities must not collide with GitHub logins or other Git names.
    label = f"{label[:100]} (git-{key[:8]})"
    print(f"unlinked Git author: {label}; generating an identicon avatar", flush=True)
    return f"git:{key}", {
        "name": label,
        "git_name": name,
        "source": "git",
    }


def fetch_contributors(repositories: list[dict], out: dict[str, Path], bindings: dict[str, tuple[str, dict]]) -> None:
    authors = {}
    people = {}
    for repo in repositories:
        local = out["root"] / repo["local_path"]
        raw = run(["git", "log", repo["revision"], "--format=%H%x09%an%x09%ae"], local, True)
        for line in raw.splitlines():
            commit, name, email = line.split("\t", 2)
            key = contributor_key(name, email)
            if key in authors:
                continue
            if key in bindings:
                person_id, person = bindings[key]
                print(f"bound Git author {name!r} to {person['name']}", flush=True)
            else:
                person_id, person = resolve_contributor(repo, commit, name, key)
            authors[key] = person_id
            if person_id in people:
                continue
            avatar = out["avatars"] / f"{person['name']}.png"
            if not avatar.exists():
                if person["source"] == "git":
                    write_identicon(avatar, key)
                else:
                    request = urllib.request.Request(person["avatar_url"], headers={"User-Agent": "darktide-mod-history"})
                    with urllib.request.urlopen(request, timeout=60) as response:
                        write_avatar(avatar, response.read())
            person["avatar"] = str(avatar.relative_to(out["root"]))
            people[person_id] = person
        print(f"resolved contributors for {repo['full_name']}: {len(people)} people so far", flush=True)
    data = {"authors": authors, "people": people}
    out["contributors"].write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def video_start_timestamp(cfg: dict) -> int:
    date = dt.date.fromisoformat(cfg["video_start_date"])
    return int(dt.datetime.combine(date, dt.time(), dt.UTC).timestamp())


def build_log(cfg: dict, out: dict[str, Path]) -> None:
    metadata = json.loads(out["metadata"].read_text(encoding="utf-8"))
    contributors = json.loads(out["contributors"].read_text(encoding="utf-8"))
    updates = load_updates(ROOT / "game-updates.toml")
    rows: list[tuple[int, str, str, str]] = []
    for repo in metadata:
        reason = repository_exclusion(repo["full_name"], cfg)
        if reason:
            print(f"excluded {repo['full_name']}: {reason}", flush=True)
            continue
        local = out["root"] / repo["local_path"]
        raw = run(["git", "-c", "core.quotepath=false", "log", repo["revision"], "--reverse", "--format=%ct%x09%an%x09%ae", "--name-status", "--no-renames"], local, True)
        timestamp = author = None
        for line in raw.splitlines():
            if not line:
                continue
            if "\t" in line and line.split("\t", 1)[0].isdigit():
                timestamp, name, email = line.split("\t", 2)
                person_id = contributors["authors"][contributor_key(name, email)]
                author = contributors["people"][person_id]["name"]
                continue
            if timestamp is None or author is None or "\t" not in line:
                raise ValueError(f"Unexpected Git log record in {repo['full_name']}: {line!r}")
            status, file_path = line.split("\t", 1)
            if status not in {"A", "M", "D", "T"}:
                raise ValueError(f"Unsupported Git file status: {status!r}")
            if file_path.startswith('"') or any(char in file_path for char in "|\t\r\n"):
                raise ValueError(f"File path cannot be represented in a Gource log: {file_path!r}")
            action = "M" if status == "T" else status
            rows.append((int(timestamp), author, action, f"/{repo['full_name']}/{file_path}"))
    rows.sort(key=lambda row: (row[0], row[1], row[3]))
    bot_names = find_bot_names(contributors["people"], cfg["bot_detection"])
    captions = build_captions(rows, updates, video_start_timestamp(cfg), bot_names)
    out["history"].write_text("\n".join("|".join(map(str, row)) for row in rows) + "\n", encoding="utf-8")
    out["captions"].write_text("\n".join(f"{timestamp}|{text}" for timestamp, text in captions) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} history entries and {len(captions)} captions")


def build_render_log(cfg: dict, out: dict[str, Path]) -> float:
    history = out["history"].read_text(encoding="utf-8").rstrip("\n")
    if not history:
        raise ValueError("Cannot render without file history")
    start = video_start_timestamp(cfg)
    if int(history.split("|", 1)[0]) < start:
        raise ValueError("File history predates video_start_date; choose an earlier video start date")
    end = int(history.rsplit("\n", 1)[-1].split("|", 1)[0])
    # Gource advances time for directory additions but creates no file or user.
    # A final anchor keeps EOF from stopping an empty scene before the last commit.
    out["render_log"].write_text(f"{start}||A|/\n{history}\n{end + 1}||A|/\n", encoding="utf-8")
    return (end + 1 - start) / 86400 * cfg["seconds_per_day"]


def render(cfg: dict, out: dict[str, Path]) -> None:
    if cfg["video_fps"] not in {25, 30, 60}:
        raise ValueError("video_fps must be 25, 30 or 60 for Gource")
    if cfg["outro_seconds"] < 0:
        raise ValueError("outro_seconds must be non-negative")
    gource = shutil.which("gource")
    if gource is None:
        raise FileNotFoundError("Gource is not on PATH")
    timeline_seconds = build_render_log(cfg, out)
    out["ppm"].unlink(missing_ok=True)
    out["video"].unlink(missing_ok=True)
    run([
        gource, "--title", cfg["gource_title"], "--hide", "filenames,progress",
        f"-{cfg['resolution']}", "--user-image-dir", str(out["avatars"]),
        "--caption-file", str(out["captions"]), "--caption-size", str(cfg["caption_size"]),
        "--caption-duration", str(cfg["caption_duration"]), "--caption-colour", cfg["caption_colour"],
        "--font-size", str(cfg["font_size"]), "--dir-font-size", str(cfg["dir_font_size"]),
        "--user-font-size", str(cfg["user_font_size"]), "--seconds-per-day", str(cfg["seconds_per_day"]),
        "--disable-auto-skip", "--log-format", "custom",
        "--dont-stop", "--stop-at-time", str(timeline_seconds + cfg["outro_seconds"]),
        "--no-vsync",
        "--output-framerate", str(cfg["video_fps"]),
        "-o", str(out["ppm"]), str(out["render_log"]),
    ], env={**os.environ, "TZ": "UTC0"})
    encode(cfg, out)


def encode(cfg: dict, out: dict[str, Path]) -> None:
    width, height = map(int, cfg["resolution"].split("x"))
    header = f"P6\n{width} {height} 255\n".encode("ascii")
    frame_size = len(header) + width * height * 3
    frame_count, remainder = divmod(out["ppm"].stat().st_size, frame_size)
    if not frame_count or remainder:
        raise ValueError("Gource PPM stream is empty or ends with an incomplete frame")
    with out["ppm"].open("rb") as stream:
        for index in range(frame_count):
            stream.seek(index * frame_size)
            if stream.read(len(header)) != header:
                raise ValueError(f"Invalid Gource PPM header at frame {index}")
    # Supply complete frames instead of asking the PNM parser to infer packet boundaries.
    run([
        "ffmpeg", "-y", "-xerror", "-f", "image2pipe", "-c:v", "ppm",
        "-frame_size", str(frame_size), "-fflags", "+noparse+nofillin",
        "-framerate", str(cfg["video_fps"]), "-i", str(out["ppm"]),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out["video"]),
    ])
    probe = json.loads(run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=nb_frames", "-of", "json", str(out["video"]),
    ], capture=True))
    if int(probe["streams"][0]["nb_frames"]) != frame_count:
        raise ValueError("Encoded video frame count does not match the Gource frames")
    print(f"encoded and verified {frame_count} frames", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["discover", "fetch", "log", "render", "all"])
    args = parser.parse_args()
    cfg = load_config()
    out = output_paths(cfg)
    if args.command in {"discover", "all"}:
        discover(cfg, out)
    if args.command in {"fetch", "all"}:
        fetch(cfg, out)
    if args.command in {"log", "all"}:
        build_log(cfg, out)
    if args.command in {"render", "all"}:
        render(cfg, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
