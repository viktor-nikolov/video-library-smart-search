#!/usr/bin/env python3
"""Debug entry point that saves the exact JPEG images sent to the VLM."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import video_descr_generator as generator


DEFAULT_DEBUG_DIR = "debug_vlm_frames"
DEBUG_DIR_ENV = "VIDEO_DESCR_DEBUG_FRAMES_DIR"
_ORIGINAL_CALL_CHATGPT = generator.call_chatgpt_for_video


def safe_path_name(value: str) -> str:
    """Convert a relative video path into a filesystem-safe folder name."""
    text = value.replace("\\", "/").strip("/")
    text = text.replace("/", "__")
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", text)
    text = text.strip(" .")
    return text or "video"


def save_vlm_frames(frames: list[bytes], rel_path: str, index: int) -> Path:
    """Write the exact frame bytes that will be embedded in the model request."""
    debug_root = Path(os.environ.get(DEBUG_DIR_ENV, DEFAULT_DEBUG_DIR)).expanduser()
    video_dir = debug_root / f"{index:05d}_{safe_path_name(rel_path)}"
    video_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "relative_path": rel_path,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "note": "Each frame_NNN.jpg is the exact JPEG byte payload sent to the VLM.",
        "frames": [],
    }

    for frame_index, frame_bytes in enumerate(frames, start=1):
        frame_name = f"frame_{frame_index:03d}.jpg"
        frame_path = video_dir / frame_name
        frame_path.write_bytes(frame_bytes)
        manifest["frames"].append(
            {
                "index": frame_index,
                "file": frame_name,
                "byte_count": len(frame_bytes),
                "sha256": hashlib.sha256(frame_bytes).hexdigest(),
            }
        )

    manifest_path = video_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return video_dir


def call_chatgpt_for_video_debug(
    frames: list[bytes],
    config: generator.AppConfig,
    logger: generator.EventLogger,
    index: int,
    total: int,
    rel_path: str,
) -> tuple[dict[str, str], int, dict[str, int | None], str | None]:
    output_dir = save_vlm_frames(frames, rel_path, index)
    logger.log(
        "INFO",
        "debug_frames_saved",
        index=index,
        total=total,
        path=rel_path,
        frames_saved=len(frames),
        output_dir=output_dir,
    )
    print(f"  Debug frames saved: {output_dir}")
    return _ORIGINAL_CALL_CHATGPT(frames, config, logger, index, total, rel_path)


def main() -> int:
    generator.call_chatgpt_for_video = call_chatgpt_for_video_debug
    return generator.main()


if __name__ == "__main__":
    raise SystemExit(main())
