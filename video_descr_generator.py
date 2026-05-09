#!/usr/bin/env python3
"""Generate structured Czech descriptions for local video files from sampled frames."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import configparser
import hashlib
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {".3gp", ".avi", ".m2ts", ".m4v", ".mov", ".mp4", ".mts"}
DEFAULT_CONFIG = "video_descr_generator.conf"


class ConfigError(Exception):
    """Raised when configuration or startup validation fails."""


class ItemProcessingError(Exception):
    """Raised when one video cannot be processed."""


@dataclass(frozen=True)
class GeneralConfig:
    input_dir: Path
    output_json: Path
    log_file: Path


@dataclass(frozen=True)
class PromptingConfig:
    prompt: str


@dataclass(frozen=True)
class VideoSamplingConfig:
    sample_interval_sec: float
    max_frames_per_video: int
    anchor_start_frames: int
    anchor_end_frames: int


@dataclass(frozen=True)
class PreprocessingConfig:
    max_dimension: int
    jpeg_quality: int
    detail: str

    @property
    def api_detail(self) -> str:
        if self.detail == "original":
            return "auto"
        return self.detail


@dataclass(frozen=True)
class ChatGPTConfig:
    api_key_env: str
    model: str
    timeout_sec: float
    max_output_tokens: int
    api_retry_count: int
    api_retry_initial_delay_sec: float
    api_retry_max_delay_sec: float


@dataclass(frozen=True)
class BehaviorConfig:
    overwrite_output: bool
    skip_if_already_present: bool
    skip_statuses: tuple[str, ...]
    flush_every_n: int
    json_retry_count: int
    continue_on_error: bool
    item_errors_for_termination: int
    max_num_of_files_processed: int
    parallel_threads: int
    heartbeat_every_n: int
    heartbeat_interval_sec: float
    use_lock_file: bool
    backup_existing_output: bool


@dataclass(frozen=True)
class AppConfig:
    general: GeneralConfig
    prompting: PromptingConfig
    video_sampling: VideoSamplingConfig
    preprocessing: PreprocessingConfig
    chatgpt: ChatGPTConfig
    behavior: BehaviorConfig


class EventLogger:
    """Small append-only line logger with the PRD-required event format."""

    def __init__(self, log_path: Path, overwrite: bool) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if overwrite else "a"
        self._handle = self.log_path.open(mode, encoding="utf-8")
        self._lock = threading.Lock()

    def close(self) -> None:
        self._handle.close()

    def log(self, level: str, event: str, **fields: Any) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        segments = [timestamp, f"{level:<7}", f"{event:<13}"]
        for key, value in fields.items():
            segments.append(f"{key}={format_log_value(value)}")
        with self._lock:
            self._handle.write(" | ".join(segments) + "\n")
            self._handle.flush()


@dataclass
class ShutdownState:
    requested: bool = False
    signal_name: str | None = None


@dataclass
class RunCounters:
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    retried: int = 0


class RunLock:
    """Advisory lock that prevents two runs from writing the same output file."""

    def __init__(self, output_json: Path) -> None:
        self.path = output_json.with_name(output_json.name + ".lock")
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        while True:
            try:
                fd = os.open(self.path, flags)
            except FileExistsError:
                pid = read_lock_pid(self.path)
                if pid is not None and process_is_running(pid):
                    raise ConfigError(
                        f"Lock file exists and process {pid} appears to be running: {self.path}"
                    )
                try:
                    self.path.unlink()
                except OSError as exc:
                    raise ConfigError(f"Cannot remove stale lock file {self.path}: {exc}") from exc
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
                handle.write(f"{datetime.now().isoformat(timespec='seconds')}\n")
            self.acquired = True
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.acquired = False


def format_log_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value)
    return text.replace("\n", " ").replace("\r", " ").replace("|", "/")


def read_lock_pid(lock_path: Path) -> int | None:
    try:
        first_line = lock_path.read_text(encoding="utf-8").splitlines()[0]
        return int(first_line)
    except (OSError, IndexError, ValueError):
        return None


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def backup_existing_output(output_json: Path) -> Path | None:
    if not output_json.exists() or output_json.is_dir():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = output_json.with_name(f"{output_json.name}.{timestamp}.bak")
    shutil.copy2(output_json, backup_path)
    return backup_path


def install_signal_handlers(shutdown: ShutdownState) -> None:
    def request_shutdown(signum: int, _frame: Any) -> None:
        shutdown.requested = True
        shutdown.signal_name = signal.Signals(signum).name

    for signal_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_shutdown)


def effective_config_snapshot(config: AppConfig, config_path: Path) -> dict[str, Any]:
    prompt_hash = hashlib.sha256(config.prompting.prompt.encode("utf-8")).hexdigest()
    return {
        "config_path": config_path.resolve(),
        "input_dir": config.general.input_dir,
        "output_json": config.general.output_json,
        "log_file": config.general.log_file,
        "model": config.chatgpt.model,
        "timeout_sec": config.chatgpt.timeout_sec,
        "max_output_tokens": config.chatgpt.max_output_tokens,
        "api_retry_count": config.chatgpt.api_retry_count,
        "api_retry_initial_delay_sec": config.chatgpt.api_retry_initial_delay_sec,
        "api_retry_max_delay_sec": config.chatgpt.api_retry_max_delay_sec,
        "sample_interval_sec": config.video_sampling.sample_interval_sec,
        "max_frames_per_video": config.video_sampling.max_frames_per_video,
        "anchor_start_frames": config.video_sampling.anchor_start_frames,
        "anchor_end_frames": config.video_sampling.anchor_end_frames,
        "max_dimension": config.preprocessing.max_dimension,
        "jpeg_quality": config.preprocessing.jpeg_quality,
        "detail": config.preprocessing.detail,
        "overwrite_output": config.behavior.overwrite_output,
        "skip_if_already_present": config.behavior.skip_if_already_present,
        "skip_statuses": ",".join(config.behavior.skip_statuses),
        "flush_every_n": config.behavior.flush_every_n,
        "json_retry_count": config.behavior.json_retry_count,
        "continue_on_error": config.behavior.continue_on_error,
        "item_errors_for_termination": config.behavior.item_errors_for_termination,
        "max_num_of_files_processed": config.behavior.max_num_of_files_processed,
        "parallel_threads": config.behavior.parallel_threads,
        "heartbeat_every_n": config.behavior.heartbeat_every_n,
        "heartbeat_interval_sec": config.behavior.heartbeat_interval_sec,
        "use_lock_file": config.behavior.use_lock_file,
        "backup_existing_output": config.behavior.backup_existing_output,
        "prompt_chars": len(config.prompting.prompt),
        "prompt_sha256": prompt_hash,
    }


def should_log_heartbeat(
    processed_count: int,
    now: float,
    last_heartbeat: float,
    config: AppConfig,
) -> bool:
    every_n = config.behavior.heartbeat_every_n
    interval = config.behavior.heartbeat_interval_sec
    by_count = every_n > 0 and processed_count > 0 and processed_count % every_n == 0
    by_time = interval > 0 and now - last_heartbeat >= interval
    return by_count or by_time


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate structured JSON descriptions for .avi, .mov, .m2ts, .mts, "
            ".mp4, .3gp, and .m4v "
            "videos by extracting image frames and sending only those images to "
            "a ChatGPT vision-capable model. Runtime settings are read from an "
            f"INI file. Default config: {DEFAULT_CONFIG}."
        )
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=DEFAULT_CONFIG,
        help=f"Path to INI configuration file. Defaults to ./{DEFAULT_CONFIG}.",
    )
    return parser.parse_args(argv)


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            f"Create {DEFAULT_CONFIG} or pass a config path, for example: "
            f"python video_descr_generator.py my_run.conf"
        )

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except configparser.Error as exc:
        raise ConfigError(f"Invalid INI config: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {exc}") from exc

    required = {
        "general": ("input_dir", "output_json", "log_file"),
        "prompting": ("prompt",),
        "video_sampling": (
            "sample_interval_sec",
            "max_frames_per_video",
            "anchor_start_frames",
            "anchor_end_frames",
        ),
        "preprocessing": ("max_dimension", "jpeg_quality", "detail"),
        "chatgpt": (
            "api_key_env",
            "model",
            "timeout_sec",
            "max_output_tokens",
            "api_retry_count",
            "api_retry_initial_delay_sec",
            "api_retry_max_delay_sec",
        ),
        "behavior": (
            "overwrite_output",
            "skip_if_already_present",
            "skip_statuses",
            "flush_every_n",
            "json_retry_count",
            "continue_on_error",
            "item_errors_for_termination",
            "max_num_of_files_processed",
            "parallel_threads",
            "heartbeat_every_n",
            "heartbeat_interval_sec",
            "use_lock_file",
            "backup_existing_output",
        ),
    }
    for section, keys in required.items():
        if not parser.has_section(section):
            raise ConfigError(f"Missing required config section [{section}]")
        for key in keys:
            if key not in parser[section] or not parser[section][key].strip():
                raise ConfigError(f"Missing required config value [{section}] {key}")

    base_dir = config_path.resolve().parent

    try:
        general = GeneralConfig(
            input_dir=resolve_config_path(parser["general"]["input_dir"], base_dir),
            output_json=resolve_config_path(parser["general"]["output_json"], base_dir),
            log_file=resolve_config_path(parser["general"]["log_file"], base_dir),
        )
        prompting = PromptingConfig(prompt=parser["prompting"]["prompt"].strip())
        video_sampling = VideoSamplingConfig(
            sample_interval_sec=parser["video_sampling"].getfloat("sample_interval_sec"),
            max_frames_per_video=parser["video_sampling"].getint("max_frames_per_video"),
            anchor_start_frames=parser["video_sampling"].getint("anchor_start_frames"),
            anchor_end_frames=parser["video_sampling"].getint("anchor_end_frames"),
        )
        preprocessing = PreprocessingConfig(
            max_dimension=parser["preprocessing"].getint("max_dimension"),
            jpeg_quality=parser["preprocessing"].getint("jpeg_quality"),
            detail=parser["preprocessing"]["detail"].strip().lower(),
        )
        chatgpt = ChatGPTConfig(
            api_key_env=parser["chatgpt"]["api_key_env"].strip(),
            model=parser["chatgpt"]["model"].strip(),
            timeout_sec=parser["chatgpt"].getfloat("timeout_sec"),
            max_output_tokens=parser["chatgpt"].getint("max_output_tokens"),
            api_retry_count=parser["chatgpt"].getint("api_retry_count"),
            api_retry_initial_delay_sec=parser["chatgpt"].getfloat("api_retry_initial_delay_sec"),
            api_retry_max_delay_sec=parser["chatgpt"].getfloat("api_retry_max_delay_sec"),
        )
        behavior = BehaviorConfig(
            overwrite_output=parser["behavior"].getboolean("overwrite_output"),
            skip_if_already_present=parser["behavior"].getboolean("skip_if_already_present"),
            skip_statuses=parse_skip_statuses(parser["behavior"]["skip_statuses"]),
            flush_every_n=parser["behavior"].getint("flush_every_n"),
            json_retry_count=parser["behavior"].getint("json_retry_count"),
            continue_on_error=parser["behavior"].getboolean("continue_on_error"),
            item_errors_for_termination=parser["behavior"].getint("item_errors_for_termination"),
            max_num_of_files_processed=parser["behavior"].getint("max_num_of_files_processed"),
            parallel_threads=parser["behavior"].getint("parallel_threads"),
            heartbeat_every_n=parser["behavior"].getint("heartbeat_every_n"),
            heartbeat_interval_sec=parser["behavior"].getfloat("heartbeat_interval_sec"),
            use_lock_file=parser["behavior"].getboolean("use_lock_file"),
            backup_existing_output=parser["behavior"].getboolean("backup_existing_output"),
        )
    except ValueError as exc:
        raise ConfigError(f"Invalid typed config value: {exc}") from exc

    config = AppConfig(
        general=general,
        prompting=prompting,
        video_sampling=video_sampling,
        preprocessing=preprocessing,
        chatgpt=chatgpt,
        behavior=behavior,
    )
    validate_config_values(config)
    return config


def resolve_config_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def parse_skip_statuses(value: str) -> tuple[str, ...]:
    statuses = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    if not statuses:
        raise ValueError("skip_statuses must contain at least one status")
    allowed = {"ok", "error"}
    invalid = sorted(set(statuses) - allowed)
    if invalid:
        raise ValueError(f"skip_statuses may contain only ok,error; invalid: {','.join(invalid)}")
    return statuses


def validate_config_values(config: AppConfig) -> None:
    if not config.general.input_dir.exists() or not config.general.input_dir.is_dir():
        raise ConfigError(
            f"Input directory does not exist or is not a directory: {config.general.input_dir}"
        )
    if config.video_sampling.sample_interval_sec <= 0:
        raise ConfigError("sample_interval_sec must be greater than 0")
    if config.video_sampling.max_frames_per_video <= 0:
        raise ConfigError("max_frames_per_video must be greater than 0")
    if config.video_sampling.anchor_start_frames < 0:
        raise ConfigError("anchor_start_frames must be zero or greater")
    if config.video_sampling.anchor_end_frames < 0:
        raise ConfigError("anchor_end_frames must be zero or greater")
    if config.preprocessing.max_dimension <= 0:
        raise ConfigError("max_dimension must be greater than 0")
    if not 1 <= config.preprocessing.jpeg_quality <= 100:
        raise ConfigError("jpeg_quality must be between 1 and 100")
    if config.preprocessing.detail not in {"low", "high", "auto", "original"}:
        raise ConfigError("detail must be one of: low, high, auto, original")
    if config.chatgpt.timeout_sec <= 0:
        raise ConfigError("timeout_sec must be greater than 0")
    if config.chatgpt.max_output_tokens <= 0:
        raise ConfigError("max_output_tokens must be greater than 0")
    if config.chatgpt.api_retry_count < 0:
        raise ConfigError("api_retry_count must be zero or greater")
    if config.chatgpt.api_retry_initial_delay_sec < 0:
        raise ConfigError("api_retry_initial_delay_sec must be zero or greater")
    if config.chatgpt.api_retry_max_delay_sec < config.chatgpt.api_retry_initial_delay_sec:
        raise ConfigError("api_retry_max_delay_sec must be greater than or equal to api_retry_initial_delay_sec")
    if config.behavior.flush_every_n <= 0:
        raise ConfigError("flush_every_n must be greater than 0")
    if config.behavior.json_retry_count < 0:
        raise ConfigError("json_retry_count must be zero or greater")
    if config.behavior.item_errors_for_termination <= 0:
        raise ConfigError("item_errors_for_termination must be greater than 0")
    if config.behavior.max_num_of_files_processed < 0:
        raise ConfigError("max_num_of_files_processed must be zero or greater")
    if config.behavior.parallel_threads <= 0:
        raise ConfigError("parallel_threads must be greater than 0")
    if config.behavior.heartbeat_every_n < 0:
        raise ConfigError("heartbeat_every_n must be zero or greater")
    if config.behavior.heartbeat_interval_sec < 0:
        raise ConfigError("heartbeat_interval_sec must be zero or greater")


def validate_startup(config: AppConfig) -> None:
    missing_tools = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing_tools:
        raise ConfigError(
            "Missing required external tool(s): "
            + ", ".join(missing_tools)
            + "\nInstall FFmpeg and make sure ffmpeg and ffprobe are available on PATH."
        )
    if not os.environ.get(config.chatgpt.api_key_env):
        raise ConfigError(
            f"Environment variable {config.chatgpt.api_key_env} is not set.\n"
            f"Set it to your OpenAI API key before running the script."
        )
    dependency_errors = []
    try:
        import openai

        if not hasattr(openai, "OpenAI"):
            dependency_errors.append(("OpenAI SDK 1.x or newer", "openai"))
    except ImportError:
        dependency_errors.append(("OpenAI SDK", "openai"))
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        dependency_errors.append(("Pillow", "Pillow"))
    if dependency_errors:
        details = "; ".join(f"{name} ({package})" for name, package in dependency_errors)
        raise ConfigError(f"Missing Python dependency: {details}\n{dependency_hint(dependency_errors)}")

    if config.general.output_json.exists():
        if not config.behavior.overwrite_output and not config.behavior.skip_if_already_present:
            raise ConfigError(
                f"Output JSON already exists and overwrite_output=false: {config.general.output_json}\n"
                "Set overwrite_output=true or skip_if_already_present=true."
            )
        if config.general.output_json.is_dir():
            raise ConfigError(f"output_json points to a directory: {config.general.output_json}")
    if config.general.log_file.exists() and config.general.log_file.is_dir():
        raise ConfigError(f"log_file points to a directory: {config.general.log_file}")

    for path in (config.general.output_json.parent, config.general.log_file.parent):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"Cannot create output directory {path}: {exc}") from exc


def dependency_hint(dependencies: list[tuple[str, str]]) -> str:
    packages = " ".join(package for _, package in dependencies)
    if platform.system().lower() == "linux":
        apt_packages = []
        for _, package in dependencies:
            if package == "Pillow":
                apt_packages.append("python3-pil")
            elif package == "openai":
                apt_packages.append("python3-openai")
        apt_text = " ".join(apt_packages)
        return (
            f"Debian/Ubuntu: sudo apt install {apt_text}\n"
            f"Python package install: python -m pip install {packages}"
        )
    return f"Install with: python -m pip install {packages}"


def scan_videos(input_dir: Path) -> list[Path]:
    videos = [
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return sorted(videos, key=lambda p: p.relative_to(input_dir).as_posix().lower())


def relative_video_path(path: Path, input_dir: Path) -> str:
    return path.relative_to(input_dir).as_posix()


def load_existing_results(output_json: Path) -> list[dict[str, Any]]:
    if not output_json.exists():
        return []
    try:
        with output_json.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot load existing output JSON for restart: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError("Existing output JSON must be a top-level array")
    return [item for item in data if isinstance(item, dict)]


def write_output_atomic(output_json: Path, results: list[dict[str, Any]]) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_json.with_name(output_json.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp_path.replace(output_json)


def remove_existing_result_for_path(results: list[dict[str, Any]], relative_path: str) -> None:
    results[:] = [item for item in results if item.get("relative_path") != relative_path]


def build_already_present(results: list[dict[str, Any]], skip_statuses: tuple[str, ...]) -> set[str]:
    return {
        item["relative_path"]
        for item in results
        if isinstance(item.get("relative_path"), str)
        and str(item.get("status", "")).lower() in skip_statuses
    }


def probe_duration(video_path: Path) -> float | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        duration = float(text)
    except ValueError:
        return None
    if duration <= 0:
        return None
    return duration


def choose_frame_timestamps(duration: float | None, sampling: VideoSamplingConfig) -> list[float]:
    max_frames = sampling.max_frames_per_video
    interval = sampling.sample_interval_sec
    if duration is None:
        return [0.0]

    if duration <= interval * max_frames:
        count = max(1, min(max_frames, int(duration // interval) + 1))
        timestamps = [i * interval for i in range(count)]
        return normalize_timestamps(timestamps, duration, max_frames, fill_uniform=False)

    start_count = min(sampling.anchor_start_frames, max_frames)
    end_count = min(sampling.anchor_end_frames, max_frames - start_count)
    middle_count = max_frames - start_count - end_count

    if middle_count <= 0:
        return normalize_timestamps(
            uniform_points(0.0, duration, max_frames),
            duration,
            max_frames,
            fill_uniform=False,
        )

    start_region_end = min(duration * 0.15, interval * max(0, start_count - 1))
    end_region_start = max(duration - min(duration * 0.15, interval * max(0, end_count - 1)), 0.0)

    start_points = uniform_points(0.0, start_region_end, start_count)
    end_points = uniform_points(end_region_start, duration, end_count)
    middle_start = min(start_region_end + interval, duration)
    middle_end = max(end_region_start - interval, middle_start)
    middle_points = uniform_points(middle_start, middle_end, middle_count)

    timestamps = start_points + middle_points + end_points
    return normalize_timestamps(timestamps, duration, max_frames, fill_uniform=True)


def uniform_points(start: float, end: float, count: int) -> list[float]:
    if count <= 0:
        return []
    if count == 1:
        return [(start + end) / 2.0]
    if end <= start:
        return [start for _ in range(count)]
    step = (end - start) / (count - 1)
    return [start + step * i for i in range(count)]


def normalize_timestamps(
    timestamps: list[float],
    duration: float,
    max_frames: int,
    fill_uniform: bool,
) -> list[float]:
    if duration <= 0:
        return [0.0]
    end_padding = min(2.0, max(1.0, duration * 0.005))
    upper = max(0.0, duration - end_padding)
    min_gap = min(0.25, max(0.02, duration / max(max_frames * 40, 1)))
    normalized: list[float] = []
    for timestamp in sorted(max(0.0, min(upper, value)) for value in timestamps):
        rounded = round(timestamp, 3)
        if not normalized or abs(rounded - normalized[-1]) >= min_gap:
            normalized.append(rounded)
        if len(normalized) >= max_frames:
            break
    if not normalized:
        normalized = [0.0]

    fill_count = max_frames - len(normalized)
    if fill_uniform and fill_count > 0 and len(normalized) < min(max_frames, max(1, int(duration))):
        for candidate in uniform_points(0.0, upper, max_frames):
            rounded = round(candidate, 3)
            if all(abs(rounded - existing) >= min_gap for existing in normalized):
                normalized.append(rounded)
            if len(normalized) >= max_frames:
                break
    return sorted(normalized[:max_frames])


def extract_frame_images(
    video_path: Path,
    timestamps: list[float],
    sampling: VideoSamplingConfig | None = None,
    duration: float | None = None,
) -> list[bytes]:
    if sampling is not None and duration is not None and can_use_fixed_interval_batch(
        timestamps,
        sampling,
        duration,
    ):
        try:
            return extract_frame_images_fixed_interval_batch(video_path, timestamps, sampling)
        except (ItemProcessingError, OSError):
            pass
    return extract_frame_images_per_timestamp(video_path, timestamps)


def can_use_fixed_interval_batch(
    timestamps: list[float],
    sampling: VideoSamplingConfig,
    duration: float,
) -> bool:
    return bool(timestamps) and duration <= sampling.sample_interval_sec * sampling.max_frames_per_video


def extract_frame_images_fixed_interval_batch(
    video_path: Path,
    timestamps: list[float],
    sampling: VideoSamplingConfig,
) -> list[bytes]:
    expected_count = len(timestamps)
    with tempfile.TemporaryDirectory(prefix="video_descr_frames_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        output_pattern = temp_dir / "frame_%03d.png"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            build_select_filter(timestamps),
            "-vsync",
            "0",
            "-frames:v",
            str(expected_count),
            "-an",
            "-f",
            "image2",
            "-vcodec",
            "png",
            str(output_pattern),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=120,
        )
        frame_files = sorted(temp_dir.glob("frame_*.png"))
        if completed.returncode != 0 or len(frame_files) != expected_count:
            error_text = completed.stderr.decode("utf-8", errors="replace").strip()
            reason = f"expected {expected_count} frames, got {len(frame_files)}"
            raise ItemProcessingError(
                "ffmpeg batch frame extraction failed: "
                + reason
                + (f": {error_text[:240]}" if error_text else "")
            )
        return [frame_path.read_bytes() for frame_path in frame_files]


def build_select_filter(timestamps: list[float]) -> str:
    conditions = []
    for index, timestamp in enumerate(timestamps):
        escaped_timestamp = f"{timestamp:.3f}"
        if index == 0:
            if timestamp <= 0.001:
                conditions.append("isnan(prev_selected_t)")
            else:
                conditions.append(f"gte(t\\,{escaped_timestamp})*isnan(prev_selected_t)")
        else:
            conditions.append(
                f"gte(t\\,{escaped_timestamp})*lt(prev_selected_t\\,{escaped_timestamp})"
            )
    return "select=" + "+".join(conditions)


def extract_frame_images_per_timestamp(video_path: Path, timestamps: list[float]) -> list[bytes]:
    frames = []
    for timestamp in timestamps:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-an",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=120,
        )
        if completed.returncode != 0 or not completed.stdout:
            error_text = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ItemProcessingError(
                "ffmpeg frame extraction failed"
                + (f": {error_text[:240]}" if error_text else "")
            )
        frames.append(completed.stdout)
    return frames


def preprocess_frames(raw_frames: list[bytes], preprocessing: PreprocessingConfig) -> list[bytes]:
    from PIL import Image

    processed = []
    for raw_frame in raw_frames:
        try:
            with Image.open(io.BytesIO(raw_frame)) as image:
                image = image.convert("RGB")
                resample_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                image.thumbnail(
                    (preprocessing.max_dimension, preprocessing.max_dimension),
                    resample_filter,
                )
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=preprocessing.jpeg_quality, optimize=True)
                processed.append(output.getvalue())
        except Exception as exc:
            raise ItemProcessingError(f"image preprocessing failed: {exc}") from exc
    return processed


def call_chatgpt_for_video(
    frames: list[bytes],
    config: AppConfig,
    logger: EventLogger,
    index: int,
    total: int,
    rel_path: str,
) -> tuple[dict[str, str], int, dict[str, int | None], str | None]:
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ[config.chatgpt.api_key_env],
        timeout=config.chatgpt.timeout_sec,
    )
    attempts_allowed = config.behavior.json_retry_count + 1
    last_error: str | None = None
    token_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    saw_usage = {"input_tokens": False, "output_tokens": False, "total_tokens": False}

    for attempt in range(1, attempts_allowed + 1):
        prompt = config.prompting.prompt
        if attempt > 1:
            prompt = (
                "Předchozí odpověď nebyla validní. Vrať pouze validní JSON objekt "
                's přesně dvěma neprázdnými řetězcovými poli "headline_cs" a '
                f'"description_cs". Nepřidávej markdown ani další text.\n\n{prompt}'
            )

        response = create_response_with_api_retries(
            client,
            prompt,
            frames,
            config,
            logger,
            index,
            total,
            rel_path,
            attempt,
        )

        usage = extract_usage(response)
        for key, value in usage.items():
            if value is not None:
                token_totals[key] += value
                saw_usage[key] = True

        raw_text = extract_response_text(response)
        try:
            validated = parse_and_validate_model_json(raw_text)
            return (
                validated,
                attempt,
                {
                    key: token_totals[key] if saw_usage[key] else None
                    for key in token_totals
                },
                last_error,
            )
        except ValueError as exc:
            last_error = str(exc)
            if attempt < attempts_allowed:
                logger.log(
                    "WARN",
                    "item_retry",
                    index=index,
                    total=total,
                    path=rel_path,
                    retry_no=attempt,
                    retry_reason=last_error,
                )
                print(f"  Retry {attempt}/{config.behavior.json_retry_count}: {last_error}", flush=True)
                continue
            raise ItemProcessingError(f"model returned invalid JSON: {last_error}") from exc

    raise ItemProcessingError("model returned invalid JSON")


def create_response_with_api_retries(
    client: Any,
    prompt: str,
    frames: list[bytes],
    config: AppConfig,
    logger: EventLogger,
    index: int,
    total: int,
    rel_path: str,
    json_attempt: int,
) -> Any:
    attempts_allowed = config.chatgpt.api_retry_count + 1
    for api_attempt in range(1, attempts_allowed + 1):
        try:
            return client.responses.create(
                model=config.chatgpt.model,
                input=[
                    {
                        "role": "user",
                        "content": build_openai_content(
                            prompt,
                            frames,
                            config.preprocessing.api_detail,
                        ),
                    }
                ],
                max_output_tokens=config.chatgpt.max_output_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "video_description",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "headline_cs": {"type": "string"},
                                "description_cs": {"type": "string"},
                            },
                            "required": ["headline_cs", "description_cs"],
                        },
                    }
                },
            )
        except Exception as exc:
            rate_limited = is_rate_limit_error(exc)
            if api_attempt >= attempts_allowed:
                logger.log(
                    "ERROR",
                    "api_rate_limit_exhausted" if rate_limited else "api_retries_exhausted",
                    index=index,
                    total=total,
                    path=rel_path,
                    json_attempt=json_attempt,
                    api_retry_count=config.chatgpt.api_retry_count,
                    attempts=api_attempt,
                    error=concise_error(exc),
                )
                raise
            delay = api_retry_delay(config, api_attempt)
            logger.log(
                "WARN",
                "api_rate_limit" if rate_limited else "api_retry",
                index=index,
                total=total,
                path=rel_path,
                json_attempt=json_attempt,
                api_retry_no=api_attempt,
                retry_delay_sec=round(delay, 2),
                error=concise_error(exc),
            )
            print(
                f"  {'API rate limit' if rate_limited else 'API retry'} "
                f"{api_attempt}/{config.chatgpt.api_retry_count} "
                f"after {delay:.1f}s: {concise_error(exc)}",
                flush=True,
            )
            if delay > 0:
                time.sleep(delay)


def api_retry_delay(config: AppConfig, api_attempt: int) -> float:
    initial = config.chatgpt.api_retry_initial_delay_sec
    maximum = config.chatgpt.api_retry_max_delay_sec
    if initial <= 0:
        return 0.0
    return min(maximum, initial * (2 ** max(0, api_attempt - 1)))


def is_rate_limit_error(exc: Exception) -> bool:
    if exc.__class__.__name__ == "RateLimitError":
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, str) and "rate" in code.lower() and "limit" in code.lower():
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    return False


def build_openai_content(prompt: str, frames: list[bytes], detail: str) -> list[dict[str, str]]:
    content = [{"type": "input_text", "text": prompt}]
    for frame in frames:
        encoded = base64.b64encode(frame).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{encoded}",
                "detail": detail,
            }
        )
    return content


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    response_dict = to_plain_data(response)
    output = response_dict.get("output", [])
    texts = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        text = part.get("text")
                        if isinstance(text, str):
                            texts.append(text)
    return "\n".join(texts).strip()


def extract_usage(response: Any) -> dict[str, int | None]:
    usage = getattr(response, "usage", None)
    usage_data = to_plain_data(usage)
    if not isinstance(usage_data, dict):
        usage_data = {}
    return {
        "input_tokens": int_or_none(usage_data.get("input_tokens")),
        "output_tokens": int_or_none(usage_data.get("output_tokens")),
        "total_tokens": int_or_none(usage_data.get("total_tokens")),
    }


def to_plain_data(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_and_validate_model_json(raw_text: str) -> dict[str, str]:
    if not raw_text:
        raise ValueError("empty_response")
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise ValueError("json_not_object")
    expected = {"headline_cs", "description_cs"}
    actual = set(parsed.keys())
    if actual != expected:
        raise ValueError("schema_field_mismatch")
    headline = parsed.get("headline_cs")
    description = parsed.get("description_cs")
    if not isinstance(headline, str) or not headline.strip():
        raise ValueError("empty_headline_cs")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("empty_description_cs")
    return {"headline_cs": headline.strip(), "description_cs": description.strip()}


def error_record(
    video_path: Path,
    config: AppConfig,
    elapsed: float,
    error: str,
    duration: float | None,
    frames_submitted: int,
    timestamps: list[float],
) -> dict[str, Any]:
    return {
        "relative_path": relative_video_path(video_path, config.general.input_dir),
        "file_name": video_path.name,
        "status": "error",
        "error_message": error[:500],
        "model": config.chatgpt.model,
        "headline_cs": None,
        "description_cs": None,
        "attempt_count": 0,
        "json_validated": False,
        "elapsed_sec": round(elapsed, 2),
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "video_duration_sec": round(duration, 3) if duration is not None else None,
        "frames_submitted": frames_submitted,
        "frame_timestamps_sec": timestamps,
    }


def process_video(
    video_path: Path,
    index: int,
    total: int,
    config: AppConfig,
    logger: EventLogger,
) -> dict[str, Any]:
    start = time.monotonic()
    duration: float | None = None
    timestamps: list[float] = []
    frames_submitted = 0
    try:
        rel_path = relative_video_path(video_path, config.general.input_dir)
        duration = probe_duration(video_path)
        timestamps = choose_frame_timestamps(duration, config.video_sampling)
        raw_frames = extract_frame_images(video_path, timestamps, config.video_sampling, duration)
        processed_frames = preprocess_frames(raw_frames, config.preprocessing)
        frames_submitted = len(processed_frames)
        if not processed_frames:
            raise ItemProcessingError("no frames extracted")
        model_result, attempts, usage, _retry_reason = call_chatgpt_for_video(
            processed_frames,
            config,
            logger,
            index,
            total,
            rel_path,
        )
        elapsed = round(time.monotonic() - start, 2)
        return {
            "relative_path": rel_path,
            "file_name": video_path.name,
            "status": "ok",
            "error_message": None,
            "model": config.chatgpt.model,
            "headline_cs": model_result["headline_cs"],
            "description_cs": model_result["description_cs"],
            "attempt_count": attempts,
            "json_validated": True,
            "elapsed_sec": elapsed,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "video_duration_sec": round(duration, 3) if duration is not None else None,
            "frames_submitted": frames_submitted,
            "frame_timestamps_sec": timestamps,
        }
    except Exception as exc:
        elapsed = time.monotonic() - start
        message = concise_error(exc)
        return error_record(
            video_path,
            config,
            elapsed,
            message,
            duration,
            frames_submitted,
            timestamps,
        )


def concise_error(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    return text.replace("\n", " ")[:500]


def summarize(results: list[dict[str, Any]], skipped: int, total_elapsed: float) -> dict[str, Any]:
    ok = sum(1 for item in results if item.get("status") == "ok")
    failed = sum(1 for item in results if item.get("status") == "error")
    retried = sum(1 for item in results if int_or_none(item.get("attempt_count")) and item["attempt_count"] > 1)
    elapsed_values = [item.get("elapsed_sec") for item in results if isinstance(item.get("elapsed_sec"), (int, float))]
    token_keys = ("input_tokens", "output_tokens", "total_tokens")
    token_totals = {
        key: sum(item.get(key) or 0 for item in results if isinstance(item.get(key), int))
        for key in token_keys
    }
    avg_elapsed = sum(elapsed_values) / len(elapsed_values) if elapsed_values else 0.0
    return {
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "retried": retried,
        "total_elapsed_sec": round(total_elapsed, 2),
        "avg_elapsed_sec": round(avg_elapsed, 2),
        "total_input_tokens": token_totals["input_tokens"],
        "total_output_tokens": token_totals["output_tokens"],
        "total_tokens": token_totals["total_tokens"],
        "avg_input_tokens": average_token(results, "input_tokens"),
        "avg_output_tokens": average_token(results, "output_tokens"),
        "avg_total_tokens": average_token(results, "total_tokens"),
    }


def average_token(results: list[dict[str, Any]], key: str) -> float:
    values = [item[key] for item in results if isinstance(item.get(key), int)]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def log_heartbeat(
    logger: EventLogger,
    index: int,
    total: int,
    counters: RunCounters,
    started: float,
    trigger: str,
) -> None:
    logger.log(
        "INFO",
        "heartbeat",
        index=index,
        total=total,
        ok=counters.ok,
        failed=counters.failed,
        skipped=counters.skipped,
        retried=counters.retried,
        elapsed_sec=round(time.monotonic() - started, 2),
        trigger=trigger,
    )


@dataclass
class SubmittedVideo:
    index: int
    total: int
    video_path: Path
    rel_path: str
    future: concurrent.futures.Future[dict[str, Any]]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config_path = Path(args.config)
    run_lock: RunLock | None = None
    try:
        config = load_config(config_path)
        validate_startup(config)
        if config.behavior.use_lock_file:
            run_lock = RunLock(config.general.output_json)
            run_lock.acquire()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        logger = EventLogger(config.general.log_file, overwrite=config.behavior.overwrite_output)
    except OSError as exc:
        if run_lock is not None:
            run_lock.release()
        print(f"ERROR: cannot open log file {config.general.log_file}: {exc}", file=sys.stderr)
        return 2
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    run_records: list[dict[str, Any]] = []
    processed_since_flush = 0
    status = "completed"
    shutdown = ShutdownState()
    counters = RunCounters()
    last_heartbeat = started
    install_signal_handlers(shutdown)
    try:
        if config.behavior.backup_existing_output and config.behavior.overwrite_output:
            backup_path = backup_existing_output(config.general.output_json)
            if backup_path is not None:
                logger.log("INFO", "backup_output", output_json=config.general.output_json, backup=backup_path)
        logger.log(
            "START",
            "run_start",
            backend="chatgpt",
            model=config.chatgpt.model,
            input_dir=config.general.input_dir,
            output_json=config.general.output_json,
            log_file=config.general.log_file,
        )
        logger.log("INFO", "config_snapshot", **effective_config_snapshot(config, config_path))
        logger.log(
            "INFO",
            "config_loaded",
            sample_interval_sec=config.video_sampling.sample_interval_sec,
            max_frames_per_video=config.video_sampling.max_frames_per_video,
            anchor_start_frames=config.video_sampling.anchor_start_frames,
            anchor_end_frames=config.video_sampling.anchor_end_frames,
            max_dimension=config.preprocessing.max_dimension,
            jpeg_quality=config.preprocessing.jpeg_quality,
            detail=config.preprocessing.detail,
            json_retry_count=config.behavior.json_retry_count,
            skip_if_already_present=config.behavior.skip_if_already_present,
            skip_statuses=",".join(config.behavior.skip_statuses),
            api_retry_count=config.chatgpt.api_retry_count,
            item_errors_for_termination=config.behavior.item_errors_for_termination,
            max_num_of_files_processed=config.behavior.max_num_of_files_processed,
            parallel_threads=config.behavior.parallel_threads,
            heartbeat_every_n=config.behavior.heartbeat_every_n,
            heartbeat_interval_sec=config.behavior.heartbeat_interval_sec,
        )

        if config.behavior.skip_if_already_present:
            results = load_existing_results(config.general.output_json)
        already_present = build_already_present(results, config.behavior.skip_statuses)

        videos = scan_videos(config.general.input_dir)
        total = len(videos)
        logger.log("INFO", "scan_complete", files_found=total)
        print(f"Files found: {total}", flush=True)

        def current_batch_target() -> int:
            target = config.behavior.parallel_threads
            if not config.behavior.continue_on_error:
                target = min(target, 1)
            else:
                remaining_errors = config.behavior.item_errors_for_termination - counters.failed
                target = min(target, remaining_errors)
            if config.behavior.max_num_of_files_processed > 0:
                remaining_files = config.behavior.max_num_of_files_processed - len(run_records)
                target = min(target, remaining_files)
            return max(0, target)

        def integrate_batch(batch: list[SubmittedVideo]) -> None:
            nonlocal last_heartbeat, processed_since_flush, status
            for submitted in batch:
                try:
                    record = submitted.future.result()
                except Exception as exc:
                    record = error_record(
                        submitted.video_path,
                        config,
                        0.0,
                        f"unexpected worker failure: {concise_error(exc)}",
                        None,
                        0,
                        [],
                    )
                remove_existing_result_for_path(results, submitted.rel_path)
                results.append(record)
                run_records.append(record)
                processed_since_flush += 1

                if record["status"] == "ok":
                    counters.ok += 1
                    if record["attempt_count"] > 1:
                        counters.retried += 1
                    logger.log(
                        "ITEM",
                        "item_done",
                        index=submitted.index,
                        total=submitted.total,
                        path=submitted.rel_path,
                        status="ok",
                        attempts=record["attempt_count"],
                        json_validated=record["json_validated"],
                        elapsed_sec=record["elapsed_sec"],
                        input_tokens=record["input_tokens"],
                        output_tokens=record["output_tokens"],
                        total_tokens=record["total_tokens"],
                        frames_submitted=record["frames_submitted"],
                    )
                    print(
                        f"  Done in {record['elapsed_sec']:.2f}s, "
                        f"frames={record['frames_submitted']}, attempts={record['attempt_count']}",
                        flush=True,
                    )
                else:
                    counters.failed += 1
                    logger.log(
                        "ERROR",
                        "item_error",
                        index=submitted.index,
                        total=submitted.total,
                        path=submitted.rel_path,
                        status="error",
                        attempts=record["attempt_count"],
                        json_validated=record["json_validated"],
                        elapsed_sec=record["elapsed_sec"],
                        input_tokens=record["input_tokens"],
                        output_tokens=record["output_tokens"],
                        total_tokens=record["total_tokens"],
                        frames_submitted=record["frames_submitted"],
                        error=record["error_message"],
                    )
                    print(f"  Error: {record['error_message']}", flush=True)
                    if not config.behavior.continue_on_error:
                        status = "failed"
                    elif counters.failed >= config.behavior.item_errors_for_termination:
                        status = "terminated_item_error_threshold"
                        logger.log(
                            "ERROR",
                            "item_error_threshold_reached",
                            index=submitted.index,
                            total=submitted.total,
                            path=submitted.rel_path,
                            item_errors=counters.failed,
                            threshold=config.behavior.item_errors_for_termination,
                        )
                        print(
                            "  Terminating early: "
                            f"item errors reached {counters.failed}/"
                            f"{config.behavior.item_errors_for_termination}",
                            flush=True,
                        )

                if (
                    status == "completed"
                    and config.behavior.max_num_of_files_processed > 0
                    and len(run_records) >= config.behavior.max_num_of_files_processed
                ):
                    status = "max_num_of_files_processed_reached"
                    logger.log(
                        "INFO",
                        "max_num_of_files_processed_reached",
                        index=submitted.index,
                        total=submitted.total,
                        path=submitted.rel_path,
                        processed=len(run_records),
                        threshold=config.behavior.max_num_of_files_processed,
                    )
                    print(
                        "  Reached configured processing limit: "
                        f"{len(run_records)}/{config.behavior.max_num_of_files_processed}",
                        flush=True,
                    )

                if processed_since_flush >= config.behavior.flush_every_n:
                    write_output_atomic(config.general.output_json, results)
                    logger.log(
                        "INFO",
                        "flush_output",
                        processed_since_last_flush=processed_since_flush,
                        output_json=config.general.output_json,
                    )
                    processed_since_flush = 0
                now = time.monotonic()
                if should_log_heartbeat(submitted.index, now, last_heartbeat, config):
                    log_heartbeat(logger, submitted.index, total, counters, started, "item")
                    last_heartbeat = now

        submitted_batch: list[SubmittedVideo] = []
        batch_target = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.behavior.parallel_threads
        ) as executor:
            for index, video_path in enumerate(videos, start=1):
                rel_path = relative_video_path(video_path, config.general.input_dir)
                if shutdown.requested:
                    status = "interrupted"
                    logger.log("WARN", "shutdown_requested", signal=shutdown.signal_name, next_path=rel_path)
                    break
                if config.behavior.skip_if_already_present and rel_path in already_present:
                    counters.skipped += 1
                    print_progress(index, total, rel_path, started, skipped=counters.skipped)
                    now = time.monotonic()
                    if should_log_heartbeat(index, now, last_heartbeat, config):
                        log_heartbeat(logger, index, total, counters, started, "skip")
                        last_heartbeat = now
                    continue

                if not submitted_batch:
                    batch_target = current_batch_target()
                    if batch_target <= 0:
                        break

                logger.log("INFO", "item_start", index=index, total=total, path=rel_path)
                print_progress(index, total, rel_path, started)
                future = executor.submit(process_video, video_path, index, total, config, logger)
                submitted_batch.append(
                    SubmittedVideo(
                        index=index,
                        total=total,
                        video_path=video_path,
                        rel_path=rel_path,
                        future=future,
                    )
                )

                if len(submitted_batch) >= batch_target:
                    integrate_batch(submitted_batch)
                    submitted_batch = []
                    batch_target = 0
                    if status != "completed":
                        break

            if submitted_batch and status == "completed":
                integrate_batch(submitted_batch)

        write_output_atomic(config.general.output_json, results)
        if processed_since_flush:
            logger.log(
                "INFO",
                "flush_output",
                processed_since_last_flush=processed_since_flush,
                output_json=config.general.output_json,
            )

        total_elapsed = time.monotonic() - started
        summary = summarize(run_records, counters.skipped, total_elapsed)
        logger.log(
            "SUMMARY",
            "run_summary",
            files_found=total,
            ok=summary["ok"],
            failed=summary["failed"],
            skipped=summary["skipped"],
            retried=summary["retried"],
            total_elapsed_sec=summary["total_elapsed_sec"],
            avg_elapsed_sec=summary["avg_elapsed_sec"],
        )
        logger.log(
            "SUMMARY",
            "run_summary",
            total_input_tokens=summary["total_input_tokens"],
            total_output_tokens=summary["total_output_tokens"],
            total_tokens=summary["total_tokens"],
            avg_input_tokens=summary["avg_input_tokens"],
            avg_output_tokens=summary["avg_output_tokens"],
            avg_total_tokens=summary["avg_total_tokens"],
        )
        logger.log("SUMMARY", "run_end", status=status)
        print(
            "Summary: "
            f"ok={summary['ok']} error={summary['failed']} skipped={summary['skipped']} "
            f"retried={summary['retried']} elapsed={summary['total_elapsed_sec']:.2f}s",
            flush=True,
        )
        successful_statuses = {"completed", "max_num_of_files_processed_reached"}
        return 0 if status in successful_statuses else 1
    except KeyboardInterrupt:
        if results or run_records or processed_since_flush:
            write_output_atomic(config.general.output_json, results)
            logger.log("INFO", "flush_output", processed_since_last_flush=processed_since_flush, output_json=config.general.output_json)
        logger.log("ERROR", "run_end", status="interrupted")
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        if results or run_records or processed_since_flush:
            try:
                write_output_atomic(config.general.output_json, results)
                logger.log(
                    "INFO",
                    "flush_output",
                    processed_since_last_flush=processed_since_flush,
                    output_json=config.general.output_json,
                )
            except Exception as flush_exc:
                logger.log("ERROR", "flush_output_failed", error=concise_error(flush_exc))
        logger.log("ERROR", "run_end", status="aborted", error=concise_error(exc))
        print(f"ERROR: unexpected abort: {concise_error(exc)}", file=sys.stderr)
        return 1
    finally:
        logger.close()
        if run_lock is not None:
            run_lock.release()


def print_progress(
    index: int,
    total: int,
    rel_path: str,
    started: float,
    skipped: int | None = None,
) -> None:
    percentage = (index / total * 100.0) if total else 100.0
    elapsed = time.monotonic() - started
    suffix = f" skipped={skipped}" if skipped is not None else ""
    print(
        f"Processing {index}/{total} ({percentage:.1f}%): {rel_path} | elapsed={elapsed:.1f}s{suffix}",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
