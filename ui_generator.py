#!/usr/bin/env python3
"""Generate the portable Windows Electron UI bundle for described videos."""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
import re


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = SCRIPT_DIR / "ui_generator.conf"
SCHEMA_VERSION = 1
EMBEDDING_DIMENSION = 384
SUPPORTED_EMBEDDING_RUNTIME = "transformers_js"


@dataclass(frozen=True)
class ArchiveConfig:
    archive_id: str
    label: str
    archive_root: Path
    input_json: Path
    relative_root: str


@dataclass(frozen=True)
class ThumbnailConfig:
    enabled: bool
    skip_existing: bool
    width: int
    height: int
    jpeg_quality: int


@dataclass(frozen=True)
class SummaryConfig:
    enabled: bool
    mock_gpt_api: bool
    model: str
    api_key_env: str
    skip_existing: bool


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool
    skip_existing: bool
    embedding_model: str
    embedding_runtime: str
    embedding_dtype: str
    embedding_batch_size: int
    embedding_cache_dir: str


@dataclass(frozen=True)
class AppConfig:
    install_dependencies: bool
    node_command: str
    npm_command: str


@dataclass(frozen=True)
class GeneratorConfig:
    primary_archive_id: str
    ui_output_dir: Path
    overwrite_ui_app: bool
    archives: list[ArchiveConfig]
    thumbnails: ThumbnailConfig
    summaries: SummaryConfig
    search: SearchConfig
    app: AppConfig


def configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the portable Electron UI for the SD and HD video description archives.",
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Config file path. Defaults to {DEFAULT_CONFIG}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional smoke-test limit across all loaded videos. Zero means all videos.",
    )
    parser.add_argument(
        "--skip-npm-install",
        action="store_true",
        help="Write app files but do not install Electron/Transformers dependencies.",
    )
    parser.add_argument(
        "--skip-embeddings",
        action="store_true",
        help="Skip embedding generation even when enabled in the config.",
    )
    parser.add_argument(
        "--skip-thumbnails",
        action="store_true",
        help="Skip thumbnail generation even when enabled in the config.",
    )
    return parser.parse_args()


def read_bool(config: configparser.ConfigParser, section: str, option: str, default: bool) -> bool:
    if not config.has_option(section, option):
        return default
    return config.getboolean(section, option)


def read_int(config: configparser.ConfigParser, section: str, option: str, default: int) -> int:
    if not config.has_option(section, option):
        return default
    return config.getint(section, option)


def read_str(config: configparser.ConfigParser, section: str, option: str, default: str = "") -> str:
    if not config.has_option(section, option):
        return default
    return config.get(section, option).strip()


def normalize_windows_path_text(value: str) -> str:
    return str(value or "").replace("/", "\\").strip("\\")


def join_runtime_path(relative_root: str, relative_path: str) -> str:
    relative_root = normalize_windows_path_text(relative_root)
    relative_path = normalize_windows_path_text(relative_path)
    if relative_root in {"", "."}:
        return f".\\{relative_path}" if relative_path else "."
    return f"{relative_root}\\{relative_path}" if relative_path else relative_root


def windows_parent(value: str) -> str:
    parent = str(PureWindowsPath(value).parent)
    return "" if parent == "." else parent


def windows_name(value: str) -> str:
    return PureWindowsPath(value).name


def archive_path(archive_root: Path, relative_path: str) -> Path:
    parts = PureWindowsPath(normalize_windows_path_text(relative_path)).parts
    return archive_root.joinpath(*parts)


def stable_video_id(archive_id: str, relative_path: str) -> str:
    hash_input = f"{archive_id}\n{normalize_windows_path_text(relative_path).casefold()}".encode("utf-8")
    return f"{archive_id}_{hashlib.sha1(hash_input).hexdigest()[:16]}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def duration_text(duration_sec: Any) -> str | None:
    if duration_sec is None:
        return None
    try:
        seconds = int(round(float(duration_sec)))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_config(path: Path) -> GeneratorConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)
    with path.open("r", encoding="utf-8-sig") as handle:
        parser.read_file(handle)

    if not parser.has_section("general"):
        raise ValueError("Config must contain a [general] section")

    primary_archive_id = read_str(parser, "general", "primary_archive_id", "SD")
    ui_output_dir = Path(read_str(parser, "general", "ui_output_dir"))
    if not str(ui_output_dir):
        raise ValueError("[general] ui_output_dir is required")

    archive_sections = [
        section for section in parser.sections() if section.lower().startswith("archive:")
    ]
    if not archive_sections:
        raise ValueError("Config must contain at least one [archive:<id>] section")

    roots: dict[str, Path] = {}
    raw_archives: dict[str, tuple[str, Path, Path]] = {}
    for section in archive_sections:
        archive_id = section.split(":", 1)[1].strip()
        if not archive_id:
            raise ValueError(f"Invalid archive section name: [{section}]")
        label = read_str(parser, section, "label", archive_id)
        archive_root = Path(read_str(parser, section, "archive_root"))
        input_json = Path(read_str(parser, section, "input_json"))
        if not str(archive_root):
            raise ValueError(f"[{section}] archive_root is required")
        if not str(input_json):
            raise ValueError(f"[{section}] input_json is required")
        roots[archive_id] = archive_root
        raw_archives[archive_id] = (label, archive_root, input_json)

    if primary_archive_id not in raw_archives:
        raise ValueError(f"primary_archive_id={primary_archive_id!r} does not match an archive section")

    primary_root = roots[primary_archive_id]
    archives: list[ArchiveConfig] = []
    for archive_id, (label, archive_root, input_json) in raw_archives.items():
        relative_root = os.path.relpath(archive_root, primary_root)
        archives.append(
            ArchiveConfig(
                archive_id=archive_id,
                label=label,
                archive_root=archive_root,
                input_json=input_json,
                relative_root=normalize_windows_path_text(relative_root),
            ),
        )
    archives.sort(key=lambda archive: (archive.archive_id != primary_archive_id, archive.archive_id))

    thumbnails = ThumbnailConfig(
        enabled=read_bool(parser, "thumbnails", "enabled", True),
        skip_existing=read_bool(parser, "thumbnails", "skip_existing_thumbnails", True),
        width=read_int(parser, "thumbnails", "width", 240),
        height=read_int(parser, "thumbnails", "height", 135),
        jpeg_quality=read_int(parser, "thumbnails", "jpeg_quality", 85),
    )
    summaries = SummaryConfig(
        enabled=read_bool(parser, "summaries", "enabled", True),
        mock_gpt_api=read_bool(parser, "summaries", "mock_gpt_api", True),
        model=read_str(parser, "summaries", "model", "gpt-5.5"),
        api_key_env=read_str(parser, "summaries", "api_key_env", "OPENAI_API_KEY"),
        skip_existing=read_bool(parser, "summaries", "skip_existing_day_summaries", True),
    )
    search = SearchConfig(
        enabled=read_bool(parser, "search", "enabled", True),
        skip_existing=read_bool(parser, "search", "skip_existing_embeddings", True),
        embedding_model=read_str(parser, "search", "embedding_model", "intfloat/multilingual-e5-small"),
        embedding_runtime=read_str(parser, "search", "embedding_runtime", SUPPORTED_EMBEDDING_RUNTIME),
        embedding_dtype=read_str(parser, "search", "embedding_dtype", "fp32"),
        embedding_batch_size=read_int(parser, "search", "embedding_batch_size", 24),
        embedding_cache_dir=read_str(parser, "search", "embedding_cache_dir", "transformers_cache"),
    )
    app = AppConfig(
        install_dependencies=read_bool(parser, "app", "install_dependencies", True),
        node_command=read_str(parser, "app", "node_command", "node"),
        npm_command=read_str(parser, "app", "npm_command", "npm"),
    )

    if thumbnails.width <= 0 or thumbnails.height <= 0:
        raise ValueError("Thumbnail width and height must be positive")
    if not 1 <= thumbnails.jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 1 and 100")
    if search.embedding_batch_size <= 0:
        raise ValueError("embedding_batch_size must be positive")
    if search.embedding_runtime != SUPPORTED_EMBEDDING_RUNTIME:
        raise ValueError(f"Unsupported embedding_runtime={search.embedding_runtime!r}")

    return GeneratorConfig(
        primary_archive_id=primary_archive_id,
        ui_output_dir=ui_output_dir,
        overwrite_ui_app=read_bool(parser, "general", "overwrite_ui_app", True),
        archives=archives,
        thumbnails=thumbnails,
        summaries=summaries,
        search=search,
        app=app,
    )


def read_json_array(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a JSON array")
    return value


def valid_datetime(
    year: int,
    month: int,
    day: int,
    hour: int | None = None,
    minute: int | None = None,
    second: int | None = None,
) -> tuple[str, str | None] | None:
    try:
        if hour is None:
            parsed_date = dt.date(year, month, day)
            return parsed_date.isoformat(), None
        parsed = dt.datetime(year, month, day, hour or 0, minute or 0, second or 0)
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.time().isoformat()


DATE_PATTERNS = [
    (
        "filename_yyyymmddhhmmss",
        re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)"),
        "high",
    ),
    (
        "filename_yyyymmdd_time",
        re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})-(\d{2})\.(\d{2})\.(\d{2})(?!\d)"),
        "high",
    ),
    (
        "folder_yyyy_mm_dd",
        re.compile(r"(?<!\d)(\d{4})_(\d{1,2})_(\d{1,2})(?!\d)"),
        "medium",
    ),
    (
        "folder_dd_mm_yyyy",
        re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)"),
        "medium",
    ),
    (
        "folder_yymmdd",
        re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)"),
        "medium",
    ),
    (
        "filename_yyyymmdd",
        re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
        "medium",
    ),
]


def infer_capture_timestamp(relative_path: str) -> dict[str, str | None]:
    normalized = normalize_windows_path_text(relative_path)
    for source, pattern, confidence in DATE_PATTERNS:
        for match in pattern.finditer(normalized):
            values = [int(part) for part in match.groups()]
            if source == "folder_yymmdd":
                year = 2000 + values[0] if values[0] <= 79 else 1900 + values[0]
                parsed = valid_datetime(year, values[1], values[2])
            elif source == "folder_dd_mm_yyyy":
                parsed = valid_datetime(values[2], values[1], values[0])
            elif source in {"folder_yyyy_mm_dd", "filename_yyyymmdd"}:
                parsed = valid_datetime(values[0], values[1], values[2])
            else:
                parsed = valid_datetime(values[0], values[1], values[2], values[3], values[4], values[5])
            if parsed is None:
                continue
            capture_date, capture_time = parsed
            return {
                "capture_date": capture_date,
                "capture_time": capture_time,
                "timestamp_source": source,
                "timestamp_confidence": confidence,
            }
    return {
        "capture_date": None,
        "capture_time": None,
        "timestamp_source": "unknown",
        "timestamp_confidence": "none",
    }


def build_embedding_text(video: dict[str, Any]) -> str:
    parts = [
        str(video.get("headline_cs") or "").strip(),
        str(video.get("description_cs") or "").strip(),
        f"File: {video.get('file_name') or ''}",
        f"Folder: {video.get('folder_relative_path') or ''}",
        f"Archive: {video.get('archive_label') or ''}",
        f"Relative path: {video.get('relative_path') or ''}",
    ]
    return "\n".join(part for part in parts if part and not part.endswith(": "))


def load_videos(config: GeneratorConfig, limit: int = 0) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for archive in config.archives:
        print(f"Loading {archive.archive_id} descriptions from {archive.input_json}")
        raw_records = read_json_array(archive.input_json)
        for raw in raw_records:
            if raw.get("status") != "ok":
                continue
            relative_path = normalize_windows_path_text(str(raw.get("relative_path") or ""))
            if not relative_path:
                continue
            file_name = str(raw.get("file_name") or windows_name(relative_path))
            folder = windows_parent(relative_path)
            video_id = stable_video_id(archive.archive_id, relative_path)
            timestamp = infer_capture_timestamp(relative_path)
            runtime_relative_path = join_runtime_path(archive.relative_root, relative_path)
            runtime_folder_path = join_runtime_path(archive.relative_root, folder) if folder else archive.relative_root
            duration_sec = raw.get("video_duration_sec")
            video = {
                "video_id": video_id,
                "archive_id": archive.archive_id,
                "archive_label": archive.label,
                "relative_path": relative_path,
                "runtime_relative_path": runtime_relative_path,
                "file_name": file_name,
                "folder_relative_path": folder,
                "runtime_folder_path": runtime_folder_path,
                "capture_date": timestamp["capture_date"],
                "capture_time": timestamp["capture_time"],
                "timestamp_source": timestamp["timestamp_source"],
                "timestamp_confidence": timestamp["timestamp_confidence"],
                "duration_sec": duration_sec,
                "duration_text": duration_text(duration_sec),
                "thumbnail_path": f"thumbnails\\{video_id}.jpg",
                "headline_cs": str(raw.get("headline_cs") or ""),
                "description_cs": str(raw.get("description_cs") or ""),
            }
            video["embedding_text"] = build_embedding_text(video)
            videos.append(video)

    videos.sort(key=video_sort_key)
    if limit:
        videos = videos[:limit]
    print(f"Loaded {len(videos)} usable video records")
    return videos


def video_sort_key(video: dict[str, Any]) -> tuple[Any, ...]:
    date_value = video.get("capture_date") or "9999-99-99"
    has_time = 0 if video.get("capture_time") else 1
    time_value = video.get("capture_time") or "99:99:99"
    return (
        date_value,
        has_time,
        time_value,
        str(video.get("file_name") or "").casefold(),
        str(video.get("relative_path") or "").casefold(),
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def build_library_manifest(config: GeneratorConfig, videos: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for video in videos:
        counts[video["archive_id"]] = counts.get(video["archive_id"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "primary_archive_id": config.primary_archive_id,
        "archives": [
            {
                "archive_id": archive.archive_id,
                "label": archive.label,
                "relative_root": archive.relative_root,
                "video_count": counts.get(archive.archive_id, 0),
            }
            for archive in config.archives
        ],
        "data_files": {
            "videos": "data\\videos.json",
            "days": "data\\days.json",
            "embeddings_manifest": "data\\embeddings_manifest.json",
            "embeddings": "data\\embeddings.f32",
        },
        "thumbnail_root": "thumbnails",
    }


def day_key(capture_date: str | None) -> str:
    return capture_date or "unknown"


def build_mock_summary(day_videos: list[dict[str, Any]]) -> str:
    archive_counts: dict[str, int] = {}
    for video in day_videos:
        archive_counts[video["archive_label"]] = archive_counts.get(video["archive_label"], 0) + 1
    archive_text = ", ".join(f"{label}: {count}" for label, count in sorted(archive_counts.items()))
    headlines = []
    seen = set()
    for video in day_videos:
        headline = str(video.get("headline_cs") or "").strip()
        if headline and headline.casefold() not in seen:
            seen.add(headline.casefold())
            headlines.append(headline)
        if len(headlines) >= 3:
            break
    topic_text = "; ".join(headlines) if headlines else "bez popisu témat"
    count = len(day_videos)
    return f"Souhrn dne: {count} videí ({archive_text}). Hlavní záběry: {topic_text}."


def summarize_with_openai(day_videos: list[dict[str, Any]], summary_config: SummaryConfig) -> str:
    api_key = os.environ.get(summary_config.api_key_env)
    if not api_key:
        raise RuntimeError(
            f"mock_gpt_api=false but environment variable {summary_config.api_key_env} is not set",
        )

    entries = []
    for video in day_videos:
        entries.append(
            {
                "archive": video.get("archive_label"),
                "file": video.get("file_name"),
                "headline_cs": video.get("headline_cs"),
                "description_cs": video.get("description_cs"),
            },
        )
    prompt = (
        "Napiš stručný český souhrn jednoho dne rodinných videozáznamů. "
        "Zmiň hlavní události, osoby nebo prostředí, neopakuj názvy souborů, "
        "pokud nejsou důležité. Odpověz jednou až třemi větami.\n\n"
        f"Video záznamy:\n{json.dumps(entries, ensure_ascii=False)}"
    )
    payload = {
        "model": summary_config.model,
        "input": prompt,
        "max_output_tokens": 250,
    }
    response_data = call_openai_responses_api(payload, api_key)

    output_text = response_data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    chunks = []
    for item in response_data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)
    summary = "\n".join(chunks).strip()
    if not summary:
        raise RuntimeError("OpenAI summary response did not contain text")
    return summary


def call_openai_responses_api(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    retry_count = 4
    delay_sec = 2.0
    last_error: Exception | None = None
    for attempt in range(1, retry_count + 1):
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == retry_count:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"OpenAI summary request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == retry_count:
                raise RuntimeError(f"OpenAI summary request failed: {exc}") from exc
        print(f"OpenAI summary request retry {attempt}/{retry_count - 1} after {delay_sec:.1f}s", flush=True)
        time.sleep(delay_sec)
        delay_sec = min(delay_sec * 2.0, 30.0)
    raise RuntimeError(f"OpenAI summary request failed: {last_error}")


def summary_cache_identity(summary_config: SummaryConfig) -> dict[str, str | bool]:
    return {
        "summary_mode": "mock" if summary_config.mock_gpt_api else "openai",
        "mock_gpt_api": summary_config.mock_gpt_api,
        "model": "mock" if summary_config.mock_gpt_api else summary_config.model,
    }


def cached_summary_is_current(
    cached: Any,
    input_hash: str,
    identity: dict[str, str | bool],
) -> bool:
    return (
        isinstance(cached, dict)
        and cached.get("input_hash") == input_hash
        and cached.get("summary")
        and cached.get("summary_mode") == identity["summary_mode"]
        and cached.get("mock_gpt_api") == identity["mock_gpt_api"]
        and cached.get("model") == identity["model"]
    )


def build_days(
    videos: list[dict[str, Any]],
    output_dir: Path,
    summary_config: SummaryConfig,
) -> list[dict[str, Any]]:
    print("Building day groups and summaries")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for video in videos:
        grouped.setdefault(day_key(video.get("capture_date")), []).append(video)

    cache_path = output_dir / "cache" / "day_summary_cache.json"
    cache = load_json_object(cache_path)
    cache_entries: dict[str, Any] = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    new_cache_entries: dict[str, Any] = {}
    days: list[dict[str, Any]] = []
    sorted_keys = sorted(grouped.keys(), key=lambda item: (item == "unknown", item))
    identity = summary_cache_identity(summary_config)
    reused_count = 0
    generated_count = 0

    for day_index, key in enumerate(sorted_keys, start=1):
        day_videos = sorted(grouped[key], key=video_sort_key)
        summary_input = [
            {
                "video_id": video["video_id"],
                "archive_id": video["archive_id"],
                "file_name": video["file_name"],
                "headline_cs": video["headline_cs"],
                "description_cs": video["description_cs"],
            }
            for video in day_videos
        ]
        input_hash = canonical_json_hash(summary_input)
        cached = cache_entries.get(key)
        if (
            summary_config.enabled
            and summary_config.skip_existing
            and cached_summary_is_current(cached, input_hash, identity)
        ):
            summary = str(cached["summary"])
            reused_count += 1
            print(
                f"Summary progress {day_index}/{len(sorted_keys)}: reused {key} "
                f"({len(day_videos)} videos)",
                flush=True,
            )
        elif not summary_config.enabled:
            summary = ""
            print(
                f"Summary progress {day_index}/{len(sorted_keys)}: disabled {key} "
                f"({len(day_videos)} videos)",
                flush=True,
            )
        elif summary_config.mock_gpt_api:
            print(
                f"Summary progress {day_index}/{len(sorted_keys)}: generating mock {key} "
                f"({len(day_videos)} videos)",
                flush=True,
            )
            summary = build_mock_summary(day_videos)
            generated_count += 1
        else:
            print(
                f"Summary progress {day_index}/{len(sorted_keys)}: generating GPT summary {key} "
                f"({len(day_videos)} videos)",
                flush=True,
            )
            summary = summarize_with_openai(day_videos, summary_config)
            generated_count += 1

        new_cache_entries[key] = {
            "input_hash": input_hash,
            "summary": summary,
            **identity,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        write_json(
            cache_path,
            {
                "schema_version": SCHEMA_VERSION,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "entries": {**cache_entries, **new_cache_entries},
            },
        )

        archives = sorted({video["archive_id"] for video in day_videos})
        archive_labels = sorted({video["archive_label"] for video in day_videos})
        folders_by_key: dict[tuple[str, str], dict[str, str]] = {}
        for video in day_videos:
            folder_key = (video["archive_id"], video.get("folder_relative_path") or "")
            folders_by_key[folder_key] = {
                "archive_id": video["archive_id"],
                "archive_label": video["archive_label"],
                "folder_relative_path": video.get("folder_relative_path") or "",
                "runtime_folder_path": video.get("runtime_folder_path") or ".",
            }

        days.append(
            {
                "date": None if key == "unknown" else key,
                "day_key": key,
                "video_count": len(day_videos),
                "archive_ids": archives,
                "archive_labels": archive_labels,
                "coverage": "both" if len(archives) > 1 else archives[0],
                "summary_cs": summary,
                "video_ids": [video["video_id"] for video in day_videos],
                "folders": sorted(
                    folders_by_key.values(),
                    key=lambda folder: (
                        folder["archive_id"],
                        folder["folder_relative_path"].casefold(),
                    ),
                ),
            },
        )

    write_json(
        cache_path,
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "entries": new_cache_entries,
        },
    )
    print(f"Prepared {len(days)} day records; summaries generated={generated_count}, reused={reused_count}")
    return days


def ffmpeg_quality(jpeg_quality: int) -> str:
    # FFmpeg's JPEG scale is inverse, roughly 2(best)-31(worst).
    q_value = round(31 - ((jpeg_quality / 100) * 29))
    return str(max(2, min(31, q_value)))


def generate_thumbnails(
    videos: list[dict[str, Any]],
    config: GeneratorConfig,
    skip_from_args: bool = False,
) -> None:
    if skip_from_args or not config.thumbnails.enabled:
        print("Thumbnail generation skipped")
        return

    print("Generating thumbnails")
    output_dir = config.ui_output_dir
    thumbnail_dir = output_dir / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "cache" / "thumbnail_manifest.json"
    cache = load_json_object(cache_path)
    old_entries: dict[str, Any] = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    new_entries: dict[str, Any] = {}
    archive_by_id = {archive.archive_id: archive for archive in config.archives}
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg was not found on PATH")

    generated = 0
    reused = 0
    missing = 0
    failed = 0
    for index, video in enumerate(videos, start=1):
        archive = archive_by_id[video["archive_id"]]
        source = archive_path(archive.archive_root, video["relative_path"])
        thumbnail_rel = normalize_windows_path_text(video["thumbnail_path"])
        thumbnail_path = output_dir.joinpath(*PureWindowsPath(thumbnail_rel).parts)
        entry: dict[str, Any] = {
            "video_id": video["video_id"],
            "archive_id": video["archive_id"],
            "source_relative_path": video["relative_path"],
            "thumbnail_path": thumbnail_rel,
        }

        if not source.exists():
            missing += 1
            entry["status"] = "missing_source"
            new_entries[video["video_id"]] = entry
            continue

        stat = source.stat()
        source_size = stat.st_size
        source_mtime_ns = stat.st_mtime_ns
        entry.update(
            {
                "source_size": source_size,
                "source_mtime_ns": source_mtime_ns,
            },
        )
        old = old_entries.get(video["video_id"])
        if (
            config.thumbnails.skip_existing
            and thumbnail_path.exists()
            and isinstance(old, dict)
            and old.get("source_size") == source_size
            and old.get("source_mtime_ns") == source_mtime_ns
        ):
            reused += 1
            entry["status"] = "reused"
            new_entries[video["video_id"]] = entry
            continue

        thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
        filter_value = (
            f"scale=w='min({config.thumbnails.width},iw)':"
            f"h='min({config.thumbnails.height},ih)':force_original_aspect_ratio=decrease"
        )
        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            filter_value,
            "-q:v",
            ffmpeg_quality(config.thumbnails.jpeg_quality),
            str(thumbnail_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and thumbnail_path.exists():
            generated += 1
            entry["status"] = "generated"
        else:
            failed += 1
            entry["status"] = "failed"
            entry["error"] = (result.stderr or result.stdout or "unknown ffmpeg error").strip()[:500]
        new_entries[video["video_id"]] = entry
        if index % 100 == 0:
            print(f"Thumbnail progress: {index} / {len(videos)}")

    write_json(
        cache_path,
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "entries": new_entries,
        },
    )
    print(
        "Thumbnail results: "
        f"generated={generated}, reused={reused}, missing_sources={missing}, failed={failed}",
    )


def ensure_app_files(config: GeneratorConfig) -> None:
    print("Generating Electron app files")
    app_dir = config.ui_output_dir / "app"
    if config.overwrite_ui_app:
        for relative in ["run_ui.bat", "run_ui.vbs", "run_ui_hidden.vbs", "run_ui_debug.bat"]:
            target = config.ui_output_dir / relative
            if target.exists():
                target.unlink()
    if config.overwrite_ui_app and app_dir.exists():
        for relative in [
            "main.js",
            "preload.js",
            "package.json",
            "run_ui.bat",
            "run_ui.vbs",
            "run_ui_hidden.vbs",
            "run_ui_debug.bat",
            "embedding\\transformers_embedding.mjs",
            "renderer\\index.html",
            "renderer\\app.js",
            "renderer\\styles.css",
        ]:
            target = app_dir.joinpath(*PureWindowsPath(relative).parts)
            if target.exists():
                target.unlink()

    (app_dir / "embedding").mkdir(parents=True, exist_ok=True)
    (app_dir / "renderer").mkdir(parents=True, exist_ok=True)
    (app_dir / config.search.embedding_cache_dir).mkdir(parents=True, exist_ok=True)
    write_text(config.ui_output_dir / "run_ui.bat", root_run_ui_bat_text(), newline="\r\n")
    write_text(config.ui_output_dir / "run_ui.vbs", root_run_ui_vbs_text(), newline="\r\n")
    write_text(config.ui_output_dir / "run_ui_debug.bat", root_run_ui_debug_bat_text(), newline="\r\n")
    write_text(app_dir / "package.json", package_json_text())
    write_text(app_dir / "main.js", main_js_text())
    write_text(app_dir / "preload.js", preload_js_text())
    write_text(app_dir / "run_ui.bat", run_ui_bat_text(), newline="\r\n")
    write_text(app_dir / "run_ui.vbs", run_ui_vbs_text(), newline="\r\n")
    write_text(app_dir / "run_ui_debug.bat", run_ui_debug_bat_text(), newline="\r\n")
    write_text(app_dir / "embedding" / "transformers_embedding.mjs", transformers_embedding_mjs_text())
    write_text(app_dir / "renderer" / "index.html", index_html_text())
    write_text(app_dir / "renderer" / "app.js", renderer_app_js_text())
    write_text(app_dir / "renderer" / "styles.css", styles_css_text())


def write_text(path: Path, text: str, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline=newline) as handle:
        handle.write(text.strip())
        handle.write(newline)


def resolve_command(command: str, fallback_candidates: list[str]) -> str:
    command = command.strip()
    if command:
        command_path = Path(command)
        if command_path.exists():
            return str(command_path)
        found = shutil.which(command)
        if found:
            return found
    for candidate in fallback_candidates:
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
        found = shutil.which(candidate)
        if found:
            return found
    return command


def install_app_dependencies(config: GeneratorConfig, skip_from_args: bool = False) -> None:
    if skip_from_args or not config.app.install_dependencies:
        print("npm install skipped")
        return
    app_dir = config.ui_output_dir / "app"
    package_lock = app_dir / "package-lock.json"
    node_modules = app_dir / "node_modules"
    if package_lock.exists() and node_modules.exists():
        print("Electron dependencies already present")
    else:
        print("Installing Electron and Transformers.js dependencies")
        npm_command = resolve_command(
            config.app.npm_command,
            [
                "npm.cmd",
                "npm.exe",
                str(Path("C:/Program Files/nodejs/npm.cmd")),
                str(Path("C:/Program Files/nodejs/npm")),
            ],
        )
        command = [npm_command, "install", "--omit=dev"]
        try:
            subprocess.run(command, cwd=app_dir, check=True)
        except FileNotFoundError as exc:
            raise RuntimeError("npm was not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"npm install failed with exit code {exc.returncode}") from exc
    ensure_electron_runtime(config)


def ensure_electron_runtime(config: GeneratorConfig) -> None:
    app_dir = config.ui_output_dir / "app"
    electron_exe = app_dir / "node_modules" / "electron" / "dist" / "electron.exe"
    if electron_exe.exists():
        return
    install_script = app_dir / "node_modules" / "electron" / "install.js"
    if not install_script.exists():
        raise RuntimeError("Electron install script is missing after npm install")
    print("Bundling Electron runtime")
    node_command = resolve_command(
        config.app.node_command,
        [
            "node.exe",
            str(Path("C:/Program Files/nodejs/node.exe")),
        ],
    )
    try:
        subprocess.run([node_command, str(install_script)], cwd=app_dir, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("Node.js was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Electron runtime installation failed with exit code {exc.returncode}") from exc
    if not electron_exe.exists():
        raise RuntimeError(f"Electron runtime was not created at {electron_exe}")


def embedding_records(videos: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "id": video["video_id"],
            "text": str(video.get("embedding_text") or build_embedding_text(video)),
        }
        for video in videos
    ]


def write_jsonl(path: Path, records: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def run_embedding_helper(
    helper_path: Path,
    input_path: Path,
    output_path: Path,
    config: GeneratorConfig,
    prefix: str,
    offline: bool,
) -> None:
    node_command = resolve_command(
        config.app.node_command,
        [
            "node.exe",
            str(Path("C:/Program Files/nodejs/node.exe")),
        ],
    )
    command = [
        node_command,
        str(helper_path),
        "--mode",
        "embed-jsonl",
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--model",
        config.search.embedding_model,
        "--dtype",
        config.search.embedding_dtype,
        "--prefix",
        prefix,
        "--batch-size",
        str(config.search.embedding_batch_size),
        "--cache-dir",
        str(config.ui_output_dir / "app" / config.search.embedding_cache_dir),
    ]
    if offline:
        command.extend(["--offline", "true"])
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("Node.js was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Transformers.js embedding failed with exit code {exc.returncode}") from exc


def load_embedding_output(path: Path) -> dict[str, list[float]]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    embeddings = value.get("embeddings")
    if not isinstance(embeddings, list):
        raise ValueError("Embedding output does not contain an embeddings array")
    return {str(item["id"]): item["embedding"] for item in embeddings}


def write_embedding_binary(path: Path, ordered_vectors: list[list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for vector in ordered_vectors:
            handle.write(struct.pack(f"<{len(vector)}f", *[float(value) for value in vector]))


def generate_embeddings(
    videos: list[dict[str, Any]],
    config: GeneratorConfig,
    skip_from_args: bool = False,
) -> None:
    if skip_from_args or not config.search.enabled:
        print("Embedding generation skipped")
        return

    print("Generating smart-search embeddings")
    records = embedding_records(videos)
    source_hash = canonical_json_hash(
        {
            "model": config.search.embedding_model,
            "dtype": config.search.embedding_dtype,
            "runtime": config.search.embedding_runtime,
            "prefix": "passage",
            "records": records,
        },
    )
    data_manifest_path = config.ui_output_dir / "data" / "embeddings_manifest.json"
    cache_manifest_path = config.ui_output_dir / "cache" / "embedding_manifest.json"
    embeddings_path = config.ui_output_dir / "data" / "embeddings.f32"
    old_cache = load_json_object(cache_manifest_path)
    if (
        config.search.skip_existing
        and embeddings_path.exists()
        and data_manifest_path.exists()
        and old_cache.get("source_hash") == source_hash
    ):
        print("Embedding cache is current")
        return

    app_dir = config.ui_output_dir / "app"
    helper_path = app_dir / "embedding" / "transformers_embedding.mjs"
    if not helper_path.exists():
        raise RuntimeError("Embedding helper is missing. Generate app files first.")

    work_dir = config.ui_output_dir / "cache" / "embedding_work"
    input_path = work_dir / "embedding_input.jsonl"
    output_path = work_dir / "embedding_output.json"
    write_jsonl(input_path, records)

    # Generation is allowed to download the model. The UI runtime later forces offline mode.
    run_embedding_helper(helper_path, input_path, output_path, config, "passage", offline=False)
    vectors_by_id = load_embedding_output(output_path)
    missing = [record["id"] for record in records if record["id"] not in vectors_by_id]
    if missing:
        raise RuntimeError(f"Missing embeddings for {len(missing)} records")

    ordered_vectors = [vectors_by_id[record["id"]] for record in records]
    dimensions = {len(vector) for vector in ordered_vectors}
    if len(dimensions) != 1:
        raise RuntimeError(f"Embedding output has inconsistent dimensions: {sorted(dimensions)}")
    dimension = dimensions.pop() if ordered_vectors else EMBEDDING_DIMENSION
    write_embedding_binary(embeddings_path, ordered_vectors)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": config.search.embedding_model,
        "runtime": config.search.embedding_runtime,
        "dtype": config.search.embedding_dtype,
        "prefix": "passage",
        "query_prefix": "query",
        "pooling": "mean",
        "normalized": True,
        "dimension": dimension,
        "count": len(records),
        "binary_path": "data\\embeddings.f32",
        "cache_dir": config.search.embedding_cache_dir,
        "records": [
            {
                "video_id": record["id"],
                "text_hash": sha256_text(record["text"]),
                "offset": index * dimension,
            }
            for index, record in enumerate(records)
        ],
    }
    write_json(data_manifest_path, manifest)
    write_json(
        cache_manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source_hash": source_hash,
            "data_manifest": "data\\embeddings_manifest.json",
            "binary_path": "data\\embeddings.f32",
        },
    )
    print(f"Wrote {len(records)} embeddings with dimension {dimension}")


def write_data_files(
    config: GeneratorConfig,
    videos: list[dict[str, Any]],
    days: list[dict[str, Any]],
) -> None:
    print("Writing generated metadata")
    data_dir = config.ui_output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    public_videos = []
    for video in videos:
        public = {key: value for key, value in video.items() if key != "embedding_text"}
        public_videos.append(public)
    write_json(data_dir / "videos.json", public_videos)
    write_json(data_dir / "days.json", days)
    write_json(data_dir / "library_manifest.json", build_library_manifest(config, videos))


def package_json_text() -> str:
    return """
{
  "name": "video-description-browser",
  "version": "1.0.0",
  "private": true,
  "description": "Portable local UI for browsing generated video descriptions.",
  "main": "main.js",
  "scripts": {
    "start": "electron ."
  },
  "dependencies": {
    "@huggingface/transformers": "latest",
    "electron": "latest"
  }
}
"""


def run_ui_bat_text() -> str:
    return r"""
@echo off
setlocal
set "APP_DIR=%~dp0."
set "ELECTRON_EXE=%~dp0node_modules\electron\dist\electron.exe"
set "ELECTRON_FLAGS=--disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --in-process-gpu"
if not exist "%ELECTRON_EXE%" (
  echo Electron runtime was not found in "%~dp0node_modules".
  echo Re-run ui_generator.py on the generator PC to bundle dependencies.
  pause
  exit /b 1
)
start "Video archiv" /D "%APP_DIR%" "%ELECTRON_EXE%" %ELECTRON_FLAGS% "%APP_DIR%"
exit /b 0
"""


def run_ui_debug_bat_text() -> str:
    return r"""
@echo off
setlocal
set "APP_DIR=%~dp0."
set "ELECTRON_EXE=%~dp0node_modules\electron\dist\electron.exe"
set "LOG=%~dp0..\run_ui.log"
set "ELECTRON_FLAGS=--disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --in-process-gpu"
set "VIDEO_ARCHIV_DEBUG_LOG=1"
call :log App debug launcher invoked from "%CD%"
echo Video archiv debug launcher
echo App directory: "%APP_DIR%"
echo Electron: "%ELECTRON_EXE%"
echo Log: "%LOG%"
echo.
if not exist "%ELECTRON_EXE%" (
  call :log ERROR: Electron runtime was not found at "%ELECTRON_EXE%"
  echo Electron runtime was not found in "%~dp0node_modules".
  echo Re-run ui_generator.py on the generator PC to bundle dependencies.
  pause
  exit /b 1
)
pushd "%APP_DIR%" || (
  call :log ERROR: Could not enter app directory "%APP_DIR%"
  echo Could not enter app directory "%APP_DIR%".
  pause
  exit /b 1
)
call :log Running Electron in debug mode
"%ELECTRON_EXE%" %ELECTRON_FLAGS% "%APP_DIR%" >> "%LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
call :log Electron exited with code %EXIT_CODE%
popd
echo.
echo Electron exited with code %EXIT_CODE%.
echo See "%LOG%" for details.
pause
exit /b %EXIT_CODE%

:log
>> "%LOG%" echo [%DATE% %TIME%] %*
exit /b 0
"""


def run_ui_vbs_text() -> str:
    return r"""
Option Explicit

Dim fso
Dim shell
Dim appDir
Dim electronExe
Dim command

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
electronExe = fso.BuildPath(appDir, "node_modules\electron\dist\electron.exe")

Function Q(value)
  Q = Chr(34) & value & Chr(34)
End Function

If Not fso.FileExists(electronExe) Then
  MsgBox "Electron runtime was not found in " & fso.BuildPath(appDir, "node_modules") & "." & vbCrLf & _
    "Re-run ui_generator.py on the generator PC to bundle dependencies.", vbCritical, "Video archiv"
  WScript.Quit 1
End If

command = Q(electronExe) & " --disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --in-process-gpu " & Q(appDir)
shell.Run command, 1, False
"""


def root_run_ui_bat_text() -> str:
    return r"""
@echo off
setlocal
set "APP_DIR=%~dp0app"
set "ELECTRON_EXE=%~dp0app\node_modules\electron\dist\electron.exe"
set "ELECTRON_FLAGS=--disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --in-process-gpu"
if not exist "%ELECTRON_EXE%" (
  echo Electron runtime was not found in "%~dp0app\node_modules".
  echo Re-run ui_generator.py on the generator PC to bundle dependencies.
  pause
  exit /b 1
)
start "Video archiv" /D "%APP_DIR%" "%ELECTRON_EXE%" %ELECTRON_FLAGS% "%APP_DIR%"
exit /b 0
"""


def root_run_ui_debug_bat_text() -> str:
    return r"""
@echo off
setlocal
set "APP_DIR=%~dp0app"
set "ELECTRON_EXE=%~dp0app\node_modules\electron\dist\electron.exe"
set "LOG=%~dp0run_ui.log"
set "ELECTRON_FLAGS=--disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --in-process-gpu"
set "VIDEO_ARCHIV_DEBUG_LOG=1"
call :log Root debug launcher invoked from "%CD%"
echo Video archiv debug launcher
echo UI data directory: "%~dp0"
echo App directory: "%APP_DIR%"
echo Electron: "%ELECTRON_EXE%"
echo Log: "%LOG%"
echo.
if not exist "%ELECTRON_EXE%" (
  call :log ERROR: Electron runtime was not found at "%ELECTRON_EXE%"
  echo Electron runtime was not found in "%~dp0app\node_modules".
  echo Re-run ui_generator.py on the generator PC to bundle dependencies.
  pause
  exit /b 1
)
pushd "%APP_DIR%" || (
  call :log ERROR: Could not enter app directory "%APP_DIR%"
  echo Could not enter app directory "%APP_DIR%".
  pause
  exit /b 1
)
call :log Running Electron in debug mode
"%ELECTRON_EXE%" %ELECTRON_FLAGS% "%APP_DIR%" >> "%LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
call :log Electron exited with code %EXIT_CODE%
popd
echo.
echo Electron exited with code %EXIT_CODE%.
echo See "%LOG%" for details.
pause
exit /b %EXIT_CODE%

:log
>> "%LOG%" echo [%DATE% %TIME%] %*
exit /b 0
"""


def root_run_ui_vbs_text() -> str:
    return r"""
Option Explicit

Dim fso
Dim shell
Dim uiDataDir
Dim appDir
Dim electronExe
Dim command

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

uiDataDir = fso.GetParentFolderName(WScript.ScriptFullName)
appDir = fso.BuildPath(uiDataDir, "app")
electronExe = fso.BuildPath(appDir, "node_modules\electron\dist\electron.exe")

Function Q(value)
  Q = Chr(34) & value & Chr(34)
End Function

If Not fso.FileExists(electronExe) Then
  MsgBox "Electron runtime was not found in " & fso.BuildPath(appDir, "node_modules") & "." & vbCrLf & _
    "Re-run ui_generator.py on the generator PC to bundle dependencies.", vbCritical, "Video archiv"
  WScript.Quit 1
End If

command = Q(electronExe) & " --disable-gpu --disable-gpu-compositing --disable-gpu-sandbox --in-process-gpu " & Q(appDir)
shell.Run command, 1, False
"""


def main_js_text() -> str:
    return r"""
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const fsSync = require("node:fs");
const fs = require("node:fs/promises");
const path = require("node:path");

const appDir = __dirname;
const uiDataDir = path.dirname(appDir);
const primaryArchiveRoot = path.dirname(uiDataDir);
const dataDir = path.join(uiDataDir, "data");
const logPath = path.join(uiDataDir, "run_ui.log");
const debugLoggingEnabled = process.env.VIDEO_ARCHIV_DEBUG_LOG === "1";
let mainWindow = null;

function appendLog(message) {
  if (!debugLoggingEnabled) {
    return;
  }
  const timestamp = new Date().toISOString();
  try {
    fsSync.appendFileSync(logPath, `[${timestamp}] ${message}\n`, "utf8");
  } catch (_error) {
    // Logging must never prevent the app from starting.
  }
}

function formatError(error) {
  if (!error) {
    return "Unknown error";
  }
  return error.stack || error.message || String(error);
}

app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-gpu-compositing");
app.commandLine.appendSwitch("disable-gpu-sandbox");
app.commandLine.appendSwitch("in-process-gpu");

function resolveRuntimePath(runtimeRelativePath) {
  const safePath = String(runtimeRelativePath || ".");
  return path.resolve(primaryArchiveRoot, safePath);
}

async function readJson(name) {
  const filePath = path.join(dataDir, name);
  const text = await fs.readFile(filePath, "utf8");
  return JSON.parse(text);
}

async function createWindow() {
  appendLog(`Creating BrowserWindow; appDir=${appDir}; uiDataDir=${uiDataDir}; primaryArchiveRoot=${primaryArchiveRoot}`);
  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 920,
    minHeight: 620,
    backgroundColor: "#eff8ff",
    show: false,
    webPreferences: {
      preload: path.join(appDir, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  win.webContents.on("render-process-gone", (_event, details) => {
    appendLog(`Renderer process gone: ${JSON.stringify(details)}`);
  });
  win.webContents.on("unresponsive", () => {
    appendLog("Renderer became unresponsive");
  });
  win.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    appendLog(`Renderer failed to load ${validatedURL}: ${errorCode} ${errorDescription}`);
  });
  mainWindow = win;
  win.on("closed", () => {
    if (mainWindow === win) {
      mainWindow = null;
    }
  });
  win.once("ready-to-show", () => {
    appendLog("BrowserWindow ready to show");
    win.show();
  });
  await win.loadFile(path.join(appDir, "renderer", "index.html"));
  appendLog("Renderer file loaded");
}

ipcMain.handle("data:load", async () => {
  const [library, videos, days] = await Promise.all([
    readJson("library_manifest.json"),
    readJson("videos.json"),
    readJson("days.json"),
  ]);
  let embeddingsManifest = null;
  let embeddingsBuffer = null;
  try {
    embeddingsManifest = await readJson("embeddings_manifest.json");
    const binaryPath = path.join(uiDataDir, embeddingsManifest.binary_path || "data\\embeddings.f32");
    const buffer = await fs.readFile(binaryPath);
    embeddingsBuffer = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
  } catch (error) {
    embeddingsManifest = null;
    embeddingsBuffer = null;
  }
  return { library, videos, days, embeddingsManifest, embeddingsBuffer };
});

ipcMain.handle("shell:open-video", async (_event, runtimeRelativePath) => {
  const target = resolveRuntimePath(runtimeRelativePath);
  return shell.openPath(target);
});

ipcMain.handle("shell:open-folder", async (_event, runtimeFolderPath) => {
  const target = resolveRuntimePath(runtimeFolderPath);
  return shell.openPath(target);
});

process.on("uncaughtException", (error) => {
  appendLog(`Uncaught exception: ${formatError(error)}`);
});

process.on("unhandledRejection", (reason) => {
  appendLog(`Unhandled rejection: ${formatError(reason)}`);
});

app.on("child-process-gone", (_event, details) => {
  appendLog(`Child process gone: ${JSON.stringify(details)}`);
});

app.on("render-process-gone", (_event, webContents, details) => {
  appendLog(`Render process gone: ${JSON.stringify(details)}`);
});

app.whenReady()
  .then(() => {
    appendLog(`Electron ready; version=${process.versions.electron}; chrome=${process.versions.chrome}; node=${process.versions.node}`);
    return createWindow();
  })
  .catch((error) => {
    appendLog(`Startup failed: ${formatError(error)}`);
    app.quit();
  });

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});
"""


def preload_js_text() -> str:
    return r"""
const { contextBridge, ipcRenderer } = require("electron");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

let embedderPromise = null;
let cachedManifest = null;

async function ensureEmbedder(manifest) {
  if (!manifest) {
    throw new Error("Embeddings are not available.");
  }
  cachedManifest = manifest;
  if (!embedderPromise) {
    const helperUrl = pathToFileURL(path.join(__dirname, "embedding", "transformers_embedding.mjs")).href;
    embedderPromise = import(helperUrl).then(async (mod) => {
      return mod.createEmbedder({
        model: cachedManifest.model,
        dtype: cachedManifest.dtype,
        cacheDir: path.join(__dirname, cachedManifest.cache_dir || "transformers_cache"),
        offline: true,
      });
    });
  }
  return embedderPromise;
}

contextBridge.exposeInMainWorld("videoLibrary", {
  loadData: () => ipcRenderer.invoke("data:load"),
  openVideo: (runtimeRelativePath) => ipcRenderer.invoke("shell:open-video", runtimeRelativePath),
  openFolder: (runtimeFolderPath) => ipcRenderer.invoke("shell:open-folder", runtimeFolderPath),
  embedQuery: async (query, manifest) => {
    const embed = await ensureEmbedder(manifest);
    const vectors = await embed([query], "query");
    return vectors[0];
  },
});
"""


def transformers_embedding_mjs_text() -> str:
    return r"""
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_MODEL = "intfloat/multilingual-e5-small";
const DEFAULT_BATCH_SIZE = 8;

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const part = argv[i];
    if (!part.startsWith("--")) {
      throw new Error(`Unexpected argument: ${part}`);
    }
    const key = part.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) {
      args[key] = "true";
    } else {
      args[key] = next;
      i += 1;
    }
  }
  return args;
}

function requireArg(args, name) {
  const value = args[name];
  if (!value) {
    throw new Error(`Missing required argument --${name}`);
  }
  return value;
}

function prefixText(text, prefix) {
  const trimmed = String(text ?? "").trim();
  if (prefix === "none") {
    return trimmed;
  }
  if (trimmed.startsWith("query:") || trimmed.startsWith("passage:")) {
    return trimmed;
  }
  return `${prefix}: ${trimmed}`;
}

function toPlainEmbeddings(tensor, expectedCount) {
  const nested = tensor.tolist();
  if (expectedCount === 1 && typeof nested[0] === "number") {
    return [nested];
  }
  if (Array.isArray(nested[0]) && typeof nested[0][0] === "number") {
    return nested;
  }
  throw new Error("Unexpected embedding tensor shape returned by Transformers.js");
}

async function loadTransformers() {
  try {
    return await import("@huggingface/transformers");
  } catch (error) {
    throw new Error(
      "Cannot load @huggingface/transformers. Re-run ui_generator.py on the generator PC. " +
        `Original error: ${error.message}`,
    );
  }
}

export async function createEmbedder(options = {}) {
  const {
    model = DEFAULT_MODEL,
    cacheDir,
    dtype,
    offline = false,
  } = options;

  const { pipeline, env } = await loadTransformers();

  if (cacheDir) {
    env.cacheDir = cacheDir;
    env.localModelPath = cacheDir;
  }
  env.allowRemoteModels = !offline;
  env.allowLocalModels = true;

  const pipelineOptions = {};
  if (cacheDir) {
    pipelineOptions.cache_dir = cacheDir;
  }
  if (offline) {
    pipelineOptions.local_files_only = true;
  }
  if (dtype && dtype !== "auto") {
    pipelineOptions.dtype = dtype;
  }

  const extractor = await pipeline("feature-extraction", model, pipelineOptions);

  return async function embedTexts(texts, prefix = "passage") {
    const prepared = texts.map((text) => prefixText(text, prefix));
    const output = await extractor(prepared, { pooling: "mean", normalize: true });
    return toPlainEmbeddings(output, prepared.length);
  };
}

async function readJsonl(filePath) {
  const content = await fs.readFile(filePath, "utf8");
  return content
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch (error) {
        throw new Error(`Invalid JSONL at line ${index + 1}: ${error.message}`);
      }
    });
}

async function writeJson(filePath, value) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, `${JSON.stringify(value)}\n`, "utf8");
}

async function runCli() {
  const args = parseArgs(process.argv.slice(2));
  const mode = requireArg(args, "mode");
  if (mode !== "embed-jsonl") {
    throw new Error(`Unsupported --mode: ${mode}`);
  }

  const inputPath = requireArg(args, "input");
  const outputPath = requireArg(args, "output");
  const model = args.model || DEFAULT_MODEL;
  const prefix = args.prefix || "passage";
  const batchSize = Number.parseInt(args["batch-size"] || String(DEFAULT_BATCH_SIZE), 10);
  const cacheDir = args["cache-dir"];
  const dtype = args.dtype || "auto";
  const offline = args.offline === "true";

  if (!Number.isInteger(batchSize) || batchSize <= 0) {
    throw new Error("--batch-size must be a positive integer");
  }

  const records = await readJsonl(inputPath);
  const embed = await createEmbedder({ model, cacheDir, dtype, offline });
  const embeddings = [];

  for (let start = 0; start < records.length; start += batchSize) {
    const batch = records.slice(start, start + batchSize);
    const vectors = await embed(
      batch.map((record) => record.text),
      prefix,
    );
    for (let i = 0; i < batch.length; i += 1) {
      embeddings.push({
        id: batch[i].id,
        embedding: vectors[i],
      });
    }
    console.error(`Embedded ${Math.min(start + batch.length, records.length)} / ${records.length}`);
  }

  await writeJson(outputPath, {
    model,
    prefix,
    dtype,
    count: embeddings.length,
    embeddings,
  });
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  runCli().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
"""


def index_html_text() -> str:
    return r"""
<!doctype html>
<html lang="cs">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Video archiv</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <div id="app">
      <header class="topbar">
        <div class="nav-buttons">
          <button id="backBtn" class="icon-btn" title="Zpět" aria-label="Zpět">‹</button>
          <button id="forwardBtn" class="icon-btn" title="Vpřed" aria-label="Vpřed">›</button>
        </div>
        <div class="brand">Video archiv</div>
        <nav class="view-tabs" aria-label="Pohled">
          <button id="datesTab" class="tab active">Dny</button>
          <button id="searchTab" class="tab">Hledat</button>
        </nav>
        <select id="archiveFilter" class="archive-filter" aria-label="Archiv"></select>
      </header>
      <main id="content" class="content">
        <section class="loading-screen" role="status" aria-live="polite">
          <div class="loading-spinner" aria-hidden="true"></div>
          <div class="loading-text">Nahrávám data</div>
        </section>
      </main>
    </div>
    <script src="app.js"></script>
  </body>
</html>
"""


def renderer_app_js_text() -> str:
    return r"""
const state = {
  library: null,
  videos: [],
  videosById: new Map(),
  days: [],
  embeddingsManifest: null,
  embeddings: null,
  view: { name: "dates" },
  backStack: [],
  forwardStack: [],
  archiveFilter: "all",
  searchQuery: "",
  searchResults: [],
  searchBusy: false,
  searchError: "",
  searchStatus: "",
  searchModelReady: false,
};

const content = document.getElementById("content");
const archiveFilter = document.getElementById("archiveFilter");
const backBtn = document.getElementById("backBtn");
const forwardBtn = document.getElementById("forwardBtn");
const datesTab = document.getElementById("datesTab");
const searchTab = document.getElementById("searchTab");

function normalizeText(value) {
  return String(value || "").toLocaleLowerCase("cs-CZ");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function thumbnailSrc(video) {
  return `../../${String(video.thumbnail_path || "").replaceAll("\\", "/")}`;
}

function dayLabel(day) {
  if (!day.date) {
    return "Bez data";
  }
  const parsed = new Date(`${day.date}T00:00:00`);
  return parsed.toLocaleDateString("cs-CZ", {
    weekday: "short",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

function formatVideoDate(dateValue) {
  if (!dateValue) {
    return "";
  }
  const match = String(dateValue).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (match) {
    return `${match[3]}.${match[2]}.${match[1]}`;
  }
  return String(dateValue).replaceAll(". ", ".");
}

function archiveBadgeText(day) {
  if (day.archive_ids.length > 1) {
    return "SD + HD";
  }
  return day.archive_ids[0] || "";
}

function filteredVideos(videos) {
  if (state.archiveFilter === "all") {
    return videos;
  }
  return videos.filter((video) => video.archive_id === state.archiveFilter);
}

function filteredDays() {
  return state.days
    .map((day) => {
      const videos = filteredVideos(day.video_ids.map((id) => state.videosById.get(id)).filter(Boolean));
      if (videos.length === 0) {
        return null;
      }
      return {
        ...day,
        video_count: videos.length,
        archive_ids: [...new Set(videos.map((video) => video.archive_id))].sort(),
        video_ids: videos.map((video) => video.video_id),
      };
    })
    .filter(Boolean);
}

function currentViewSnapshot() {
  return {
    ...state.view,
    scrollY: window.scrollY || document.documentElement.scrollTop || 0,
  };
}

function setView(nextView, pushHistory = true) {
  if (pushHistory) {
    state.backStack.push(currentViewSnapshot());
    state.forwardStack = [];
  }
  state.view = { ...nextView, scrollY: nextView.scrollY || 0 };
  render();
}

function renderShellState() {
  backBtn.disabled = state.backStack.length === 0;
  forwardBtn.disabled = state.forwardStack.length === 0;
  datesTab.classList.toggle("active", state.view.name === "dates" || state.view.name === "day");
  searchTab.classList.toggle("active", state.view.name === "search");
}

function render() {
  renderShellState();
  if (state.view.name === "day") {
    renderDay(state.view.dayKey);
  } else if (state.view.name === "search") {
    renderSearch();
  } else {
    renderDates();
  }
  requestAnimationFrame(() => {
    window.scrollTo({ top: state.view.scrollY || 0, left: 0, behavior: "auto" });
  });
}

function renderDates() {
  const days = filteredDays();
  content.innerHTML = `
    <section class="list-page">
      <div class="page-heading">
        <h1>Dny</h1>
        <span>${days.length} dnů</span>
      </div>
      <div class="day-list">
        ${days.map(renderDayRow).join("")}
      </div>
    </section>
  `;
  content.querySelectorAll("[data-day-key]").forEach((button) => {
    button.addEventListener("click", () => setView({ name: "day", dayKey: button.dataset.dayKey }));
  });
  attachFolderButtons();
}

function renderDayRow(day) {
  const folders = folderButtonsHtml(day.folders || []);
  return `
    <article class="day-row">
      <button class="day-main" data-day-key="${escapeHtml(day.day_key)}">
        <span class="day-date">${escapeHtml(dayLabel(day))}</span>
        <span class="badge">${escapeHtml(archiveBadgeText(day))}</span>
        <span class="count">${day.video_count} videí</span>
        <span class="summary">${escapeHtml(day.summary_cs || "")}</span>
      </button>
      <div class="folder-actions">${folders}</div>
    </article>
  `;
}

function folderButtonsHtml(folders) {
  const filtered = state.archiveFilter === "all"
    ? folders
    : folders.filter((folder) => folder.archive_id === state.archiveFilter);
  return filtered.slice(0, 4).map((folder) => `
    <button class="folder-btn" data-folder="${escapeHtml(folder.runtime_folder_path)}" title="${escapeHtml(folder.folder_relative_path || folder.archive_label)}">
      Složka ${escapeHtml(folder.archive_id)}
    </button>
  `).join("");
}

function renderDay(dayKeyValue) {
  const day = state.days.find((item) => item.day_key === dayKeyValue);
  if (!day) {
    content.innerHTML = `<section class="empty">Den nebyl nalezen.</section>`;
    return;
  }
  const videos = filteredVideos(day.video_ids.map((id) => state.videosById.get(id)).filter(Boolean));
  content.innerHTML = `
    <section class="detail-page">
      <div class="page-heading">
        <div>
          <h1>${escapeHtml(dayLabel(day))}</h1>
          <p>${escapeHtml(day.summary_cs || "")}</p>
        </div>
        <span>${videos.length} videí</span>
      </div>
      <div class="video-grid">
        ${videos.map(renderVideoCard).join("")}
      </div>
    </section>
  `;
  attachVideoButtons();
  attachFolderButtons();
  attachDescriptionToggles();
}

function renderVideoCard(video, options = {}) {
  const time = video.capture_time ? video.capture_time.slice(0, 5) : "";
  const date = options.showDate ? formatVideoDate(video.capture_date) : "";
  const timestamp = [date, time].filter(Boolean).join(" ");
  const lengthText = formatDuration(video.duration_sec, video.duration_text);
  return `
    <article class="video-card">
      <div class="thumb-column">
        <button class="thumb-button" data-video="${escapeHtml(video.video_id)}" title="${escapeHtml(video.file_name)}">
          <img src="${thumbnailSrc(video)}" alt="">
        </button>
        <div class="duration-line"><span>délka</span> ${escapeHtml(lengthText || "")}</div>
      </div>
      <div class="video-meta">
        <div class="video-topline">
          <span class="badge">${escapeHtml(video.archive_id)}</span>
          <span>${escapeHtml(timestamp)}</span>
        </div>
        <h2>${escapeHtml(video.headline_cs || video.file_name)}</h2>
        <div class="path-line">${escapeHtml(video.relative_path)}</div>
        <div class="card-actions">
          <button class="action-btn" data-video="${escapeHtml(video.video_id)}">Otevřít</button>
          <button class="action-btn" data-folder="${escapeHtml(video.runtime_folder_path)}">Složka</button>
        </div>
      </div>
      <p class="video-description" data-description tabindex="0" role="button" aria-label="Zobrazit nebo skrýt celý popis">${escapeHtml(video.description_cs || "")}</p>
    </article>
  `;
}

function formatDuration(durationSec, fallback) {
  const value = Number(durationSec);
  if (Number.isFinite(value)) {
    const totalSeconds = Math.max(0, Math.round(value));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }
  if (!fallback) {
    return "";
  }
  const parts = String(fallback).split(":").map((part) => Number.parseInt(part, 10));
  if (parts.length === 3 && parts.every(Number.isFinite)) {
    return `${parts[0] * 60 + parts[1]}:${String(parts[2]).padStart(2, "0")}`;
  }
  return String(fallback);
}

function renderSearch() {
  const emptyText = state.searchBusy
    ? ""
    : (state.searchQuery ? "Žádné výsledky." : "");
  const resultsHtml = state.searchResults.length
    ? `<div class="video-grid">${state.searchResults.map((item) => renderSearchCard(item)).join("")}</div>`
    : `<div class="empty">${emptyText}</div>`;
  content.innerHTML = `
    <section class="search-page">
      <div class="search-row">
        <input id="searchInput" class="search-input" value="${escapeHtml(state.searchQuery)}" placeholder="Hledat ve videích" ${state.searchBusy ? "disabled" : ""} autofocus>
        <button id="searchButton" class="primary-btn" ${state.searchBusy ? "disabled" : ""}>${state.searchBusy ? "Hledám" : "Hledat"}</button>
      </div>
      ${state.searchStatus ? `<div class="search-status" role="status" aria-live="polite">${escapeHtml(state.searchStatus)}</div>` : ""}
      ${state.searchError ? `<div class="error">${escapeHtml(state.searchError)}</div>` : ""}
      ${resultsHtml}
    </section>
  `;
  const input = document.getElementById("searchInput");
  if (!state.searchBusy) {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      runSearch(input.value);
    }
  });
  document.getElementById("searchButton").addEventListener("click", () => runSearch(input.value));
  attachVideoButtons();
  attachFolderButtons();
  attachDescriptionToggles();
}

function renderSearchCard(item) {
  return renderVideoCard(item.video, { showDate: true }).replace(
    '<div class="video-topline">',
    `<div class="score-line">Skóre ${item.score.toFixed(3)} · sémantika ${item.semantic.toFixed(3)} · text ${item.lexical.toFixed(3)}</div><div class="video-topline">`,
  );
}

function attachVideoButtons() {
  content.querySelectorAll("[data-video]").forEach((button) => {
    button.addEventListener("click", () => {
      const video = state.videosById.get(button.dataset.video);
      if (video) {
        window.videoLibrary.openVideo(video.runtime_relative_path);
      }
    });
  });
}

function attachFolderButtons() {
  content.querySelectorAll("[data-folder]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      window.videoLibrary.openFolder(button.dataset.folder);
    });
  });
}

function attachDescriptionToggles() {
  content.querySelectorAll("[data-description]").forEach((description) => {
    const toggle = () => description.classList.toggle("expanded");
    description.addEventListener("click", toggle);
    description.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
  });
}

function dotProduct(queryVector, videoIndex) {
  const dimension = state.embeddingsManifest.dimension;
  const offset = videoIndex * dimension;
  let score = 0;
  for (let i = 0; i < dimension; i += 1) {
    score += queryVector[i] * state.embeddings[offset + i];
  }
  return score;
}

function lexicalScore(query, video) {
  const terms = normalizeText(query).split(/\s+/).filter(Boolean);
  if (!terms.length) {
    return 0;
  }
  const fields = [
    video.file_name,
    video.folder_relative_path,
    video.relative_path,
    video.archive_label,
    video.headline_cs,
    video.description_cs,
  ];
  const haystack = normalizeText(fields.join(" "));
  let score = 0;
  for (const term of terms) {
    if (normalizeText(video.file_name).includes(term)) {
      score += 1.1;
    } else if (normalizeText(video.folder_relative_path).includes(term)) {
      score += 0.9;
    } else if (haystack.includes(term)) {
      score += 0.6;
    }
  }
  if (haystack.includes(normalizeText(query))) {
    score += 1.2;
  }
  return Math.min(1, score / Math.max(1, terms.length));
}

async function runSearch(query) {
  if (state.searchBusy) {
    return;
  }
  state.searchQuery = query.trim();
  state.searchError = "";
  state.searchStatus = "";
  state.searchBusy = true;
  render();
  try {
    if (!state.searchQuery) {
      state.searchResults = [];
      return;
    }
    const candidates = filteredVideos(state.videos);
    let queryVector = null;
    if (state.embeddings && state.embeddingsManifest) {
      if (!state.searchModelReady) {
        state.searchStatus = "Načítám model pro chytré vyhledávání z disku. Při prvním hledání přes Wi-Fi to může chvíli trvat.";
        render();
        await new Promise((resolve) => requestAnimationFrame(resolve));
      } else {
        state.searchStatus = "Hledám ve videích.";
        render();
      }
      queryVector = await window.videoLibrary.embedQuery(state.searchQuery, state.embeddingsManifest);
      state.searchModelReady = true;
      state.searchStatus = "Řadím výsledky.";
      render();
    }
    const indexById = new Map((state.embeddingsManifest?.records || []).map((record, index) => [record.video_id, index]));
    state.searchResults = candidates
      .map((video) => {
        const lexical = lexicalScore(state.searchQuery, video);
        const semantic = queryVector && indexById.has(video.video_id)
          ? dotProduct(queryVector, indexById.get(video.video_id))
          : 0;
        const score = queryVector ? semantic + lexical * 0.28 : lexical;
        return { video, lexical, semantic, score };
      })
      .filter((item) => item.score > 0)
      .sort((left, right) => right.score - left.score)
      .slice(0, 80);
  } catch (error) {
    state.searchError = error.message || String(error);
    state.searchResults = [];
  } finally {
    state.searchBusy = false;
    state.searchStatus = "";
    render();
  }
}

function setupFilters() {
  archiveFilter.innerHTML = [
    `<option value="all">SD + HD</option>`,
    ...state.library.archives.map((archive) => (
      `<option value="${escapeHtml(archive.archive_id)}">${escapeHtml(archive.label)}</option>`
    )),
  ].join("");
  archiveFilter.value = state.archiveFilter;
  archiveFilter.addEventListener("change", () => {
    state.archiveFilter = archiveFilter.value;
    render();
  });
}

async function init() {
  const data = await window.videoLibrary.loadData();
  state.library = data.library;
  state.videos = data.videos;
  state.videosById = new Map(state.videos.map((video) => [video.video_id, video]));
  state.days = data.days;
  state.embeddingsManifest = data.embeddingsManifest;
  if (data.embeddingsBuffer && data.embeddingsManifest) {
    state.embeddings = new Float32Array(data.embeddingsBuffer);
  }
  setupFilters();
  backBtn.addEventListener("click", () => {
    if (state.backStack.length) {
      state.forwardStack.push(currentViewSnapshot());
      state.view = state.backStack.pop();
      render();
    }
  });
  forwardBtn.addEventListener("click", () => {
    if (state.forwardStack.length) {
      state.backStack.push(currentViewSnapshot());
      state.view = state.forwardStack.pop();
      render();
    }
  });
  datesTab.addEventListener("click", () => setView({ name: "dates" }));
  searchTab.addEventListener("click", () => setView({ name: "search" }));
  render();
}

init().catch((error) => {
  content.innerHTML = `<section class="empty error">${escapeHtml(error.stack || error.message || error)}</section>`;
});
"""


def styles_css_text() -> str:
    return r"""
:root {
  color-scheme: light;
  --bg: #eff8ff;
  --band: #dff0fb;
  --panel: #ffffff;
  --line: #b8d8ea;
  --text: #163244;
  --muted: #60798a;
  --strong: #0a638f;
  --accent: #1683b8;
  --accent-dark: #0d5e86;
  --shadow: 0 10px 24px rgba(24, 94, 126, 0.14);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-width: 880px;
  background: var(--bg);
  color: var(--text);
  font-family: "Segoe UI", Arial, sans-serif;
}

button,
input,
select {
  font: inherit;
}

button {
  cursor: pointer;
}

button:disabled {
  cursor: default;
  opacity: 0.45;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  align-items: center;
  gap: 14px;
  min-height: 64px;
  padding: 10px 18px;
  background: #f7fcff;
  border-bottom: 1px solid var(--line);
}

.nav-buttons,
.view-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
}

.brand {
  font-size: 20px;
  font-weight: 700;
  color: var(--accent-dark);
}

.icon-btn,
.tab,
.archive-filter,
.action-btn,
.folder-btn,
.primary-btn {
  min-height: 38px;
  border: 1px solid var(--line);
  background: #ffffff;
  color: var(--text);
  border-radius: 6px;
}

.icon-btn {
  width: 40px;
  font-size: 26px;
  line-height: 1;
}

.tab {
  padding: 0 16px;
  font-weight: 650;
}

.tab.active,
.primary-btn {
  background: var(--accent);
  border-color: var(--accent);
  color: #ffffff;
}

.archive-filter {
  min-width: 150px;
  padding: 0 10px;
}

.content {
  padding: 22px;
}

.loading-screen {
  min-height: calc(100vh - 108px);
  display: grid;
  place-items: center;
  align-content: center;
  gap: 16px;
  color: var(--accent-dark);
}

.loading-spinner {
  width: 44px;
  height: 44px;
  border: 4px solid #c3e3f3;
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: loading-spin 0.9s linear infinite;
}

.loading-text {
  font-size: 19px;
  font-weight: 700;
}

@keyframes loading-spin {
  to {
    transform: rotate(360deg);
  }
}

.list-page {
  max-width: 1480px;
  margin: 0 auto;
}

.detail-page,
.search-page {
  max-width: 1920px;
  margin: 0 auto;
}

.page-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin: 8px 0 18px;
}

.page-heading h1 {
  margin: 0;
  font-size: 30px;
  line-height: 1.12;
}

.page-heading p {
  max-width: 980px;
  margin: 8px 0 0;
  color: var(--muted);
  line-height: 1.45;
}

.day-list {
  display: grid;
  gap: 10px;
}

.day-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: stretch;
  gap: 10px;
}

.day-main {
  display: grid;
  grid-template-columns: 170px 86px 86px minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  width: 100%;
  min-height: 70px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--text);
  text-align: left;
  box-shadow: var(--shadow);
}

.day-main:hover,
.video-card:hover {
  border-color: #7fc0df;
}

.day-date {
  font-weight: 750;
}

.badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 58px;
  min-height: 26px;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--band);
  color: var(--accent-dark);
  font-size: 13px;
  font-weight: 750;
}

.count,
.summary,
.path-line,
.score-line {
  color: var(--muted);
}

.summary {
  overflow: hidden;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.folder-actions,
.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.folder-btn,
.action-btn,
.primary-btn {
  padding: 0 12px;
  font-weight: 650;
}

.video-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: 14px;
}

.video-card {
  display: grid;
  grid-template-columns: 156px minmax(0, 1fr);
  gap: 14px;
  min-height: 180px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
}

.thumb-button {
  width: 156px;
  height: 92px;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #d7eaf5;
}

.thumb-column {
  width: 156px;
}

.thumb-button img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.duration-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 28px;
  margin-top: 8px;
  padding: 0 2px;
  color: var(--text);
  font-size: 13px;
  font-weight: 700;
}

.duration-line span {
  color: var(--muted);
  font-weight: 650;
}

.video-meta {
  min-width: 0;
}

.video-topline,
.score-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 28px;
  font-size: 13px;
}

.video-card h2 {
  margin: 5px 0 7px;
  font-size: 17px;
  line-height: 1.25;
}

.video-description {
  grid-column: 1 / -1;
  margin: 0;
  color: #314a5b;
  line-height: 1.38;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
}

.video-description:focus {
  outline: 2px solid #7fc0df;
  outline-offset: 3px;
}

.video-description.expanded {
  display: block;
  -webkit-line-clamp: unset;
  overflow: visible;
}

.path-line {
  margin-top: 8px;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.card-actions {
  margin-top: 10px;
}

.search-row {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) auto;
  gap: 10px;
  margin-bottom: 16px;
}

.search-input {
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--text);
}

.primary-btn {
  min-width: 110px;
}

.search-status {
  margin-bottom: 16px;
  padding: 12px 14px;
  border: 1px solid #b7d8ea;
  border-radius: 8px;
  background: #e9f6fb;
  color: #184a5c;
  font-size: 14px;
}

.empty,
.error {
  padding: 28px;
  color: var(--muted);
}

.error {
  color: #a33a3a;
}

@media (max-width: 980px) {
  body {
    min-width: 0;
  }

  .topbar {
    grid-template-columns: auto 1fr;
  }

  .view-tabs,
  .archive-filter {
    grid-column: 1 / -1;
  }

  .day-row,
  .day-main,
  .video-card,
  .search-row {
    grid-template-columns: 1fr;
  }

  .folder-actions {
    padding-left: 2px;
  }

  .thumb-button {
    width: 100%;
    height: auto;
    aspect-ratio: 16 / 9;
  }

  .thumb-column {
    width: 100%;
  }
}
"""


def main() -> int:
    configure_stdout()
    args = parse_args()
    if args.limit < 0:
        raise ValueError("--limit must be zero or greater")
    config = parse_config(args.config)

    print(f"UI output: {config.ui_output_dir}")
    config.ui_output_dir.mkdir(parents=True, exist_ok=True)
    (config.ui_output_dir / "data").mkdir(parents=True, exist_ok=True)
    (config.ui_output_dir / "cache").mkdir(parents=True, exist_ok=True)

    videos = load_videos(config, limit=args.limit)
    ensure_app_files(config)
    install_app_dependencies(config, skip_from_args=args.skip_npm_install)
    generate_thumbnails(videos, config, skip_from_args=args.skip_thumbnails)
    days = build_days(videos, config.ui_output_dir, config.summaries)
    write_data_files(config, videos, days)
    generate_embeddings(videos, config, skip_from_args=args.skip_embeddings)
    print("UI generation complete")
    print(f"Launch with: {config.ui_output_dir / 'run_ui.bat'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
