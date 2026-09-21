from __future__ import annotations

import datetime as dt
from pathlib import Path
import tomllib

def load_updates(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        entries = tomllib.load(stream)["updates"]
    updates = []
    names = set()
    for entry in entries:
        name = entry["name"]
        released_at = entry["released_at"]
        if not name.strip() or any(char in name for char in "|\r\n"):
            raise ValueError(f"Invalid update name: {name!r}")
        if name in names:
            raise ValueError(f"Duplicate update: {name}")
        if not isinstance(released_at, dt.datetime) or released_at.utcoffset() is None:
            raise ValueError(f"Update requires a timezone-aware released_at: {name}")
        names.add(name)
        updates.append({"timestamp": int(released_at.timestamp()), "title": name})
    return sorted(updates, key=lambda update: update["timestamp"])


def find_bot_names(people: dict[str, dict], rules: dict) -> set[str]:
    normalized = {}
    for key in ("github_types", "git_name_suffixes", "bot_names", "human_names"):
        values = rules[key]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in values
        ):
            raise ValueError(f"bot_detection.{key} must be a list of non-empty strings without surrounding whitespace")
        normalized[key] = {value.casefold() for value in values}
    overlap = normalized["bot_names"] & normalized["human_names"]
    if overlap:
        raise ValueError(f"Names cannot be both bots and humans: {', '.join(sorted(overlap))}")
    result = set()
    for person in people.values():
        name = person["name"].casefold()
        if name in normalized["human_names"]:
            continue
        if name in normalized["bot_names"]:
            result.add(person["name"])
            continue
        if person["source"] == "github":
            matched = person["github_type"].casefold() in normalized["github_types"]
        elif person["source"] == "git":
            matched = person["git_name"].casefold().endswith(tuple(normalized["git_name_suffixes"]))
        else:
            raise ValueError(f"Unsupported contributor source: {person['source']!r}")
        if matched:
            result.add(person["name"])
    return result


def build_captions(rows: list[tuple], updates: list[dict], start: int, bot_names: set[str]) -> list[tuple[int, str]]:
    if not rows:
        raise ValueError("Cannot build captions without file history")
    if min(row[0] for row in rows) < start:
        raise ValueError("File history predates video_start_date; choose an earlier video start date")
    first_seen: dict[str, int] = {}
    for timestamp, person, _, _ in rows:
        if person in bot_names:
            continue
        first_seen[person] = min(timestamp, first_seen.get(person, timestamp))
    end = max(row[0] for row in rows)
    captions = [(timestamp, f"{person} joins") for person, timestamp in first_seen.items()]
    captions.extend(
        (update["timestamp"], update["title"])
        for update in updates if start <= update["timestamp"] <= end
    )
    return sorted(captions)
