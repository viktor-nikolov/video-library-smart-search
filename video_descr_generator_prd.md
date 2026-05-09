# Product Requirements Document: `video_descr_generator.py`

## 1. Purpose

Build a Python proof-of-concept script that generates **structured JSON descriptions for video files** in a local video collection using a ChatGPT vision-capable model.

The model will **not process audio**. The description must be produced **only from image frames extracted from the video**.

The script must generate one JSON record per video and must provide sufficient logging and observability so that the implementation can be produced by Codex in one go.

---

## 2. Goals and Non-Goals

### 2.1 Primary goals

- Process all `.avi`, `.mov`, `.m2ts`, `.mts`, `.mp4`, `.3gp`, and `.m4v` files in a target directory and all subdirectories.
- Extract representative image frames from each video.
- Send those frames to a ChatGPT vision-capable model.
- Produce one structured JSON description per video file.
- Return **exactly two VLM-generated text fields** per successfully processed video:
  - a short Czech headline
  - a Czech description of people, activities, and environment visible in the sampled frames
- Produce a processing log with per-file timing, retry information, and summary statistics.
- Show progress on the console during execution.
- Keep runtime configuration outside command-line arguments, except for the config file path.
- Make backend usage visible: processing time and token usage.

### 2.2 Secondary goals

- Allow deterministic, repeatable processing.
- Allow safe restart of interrupted runs.
- Make the output easy to consume by downstream indexing or embedding pipelines.

### 2.3 Non-goals for v1

- No audio transcription.
- No speech recognition.
- No direct video upload to the model.
- No GUI.
- No multiprocessing or distributed execution.
- No database integration.
- No scene-detection or advanced video analytics unless added later.

---

## 3. Deliverables

### 3.1 Required files

A Python script named:

```text
video_descr_generator.py
```

and a configuration file, by default:

```text
video_descr_generator.conf
```

A debug entry point is also provided:

```text
video_descr_generator_debug.py
```

This debug script must use the same processing pipeline as `video_descr_generator.py`, but additionally save the exact post-preprocessing JPEG image bytes that are sent to the VLM request.

### 3.2 Expected implementation style

The implementation may be delivered as:

- a single Python script, or
- a small Python package with helper modules,

but the entry point must be executable as:

```bash
python video_descr_generator.py
```

The debug entry point must be executable as:

```bash
python video_descr_generator_debug.py
```

---

## 4. Functional Overview

The script must operate as follows:

1. Load configuration from an INI config file.
2. Validate startup requirements (on error, exit gracefully and print to console a short instructions to the user how to fix the issue):
   - required config fields are present
   - API key environment variable exists
   - `ffmpeg` and `ffprobe` are available
   - output path behavior is valid
3. Recursively scan the configured input directory for supported video files.
4. Sort files deterministically.
5. For each video file:
   - determine basic metadata (path, file name, duration if available)
   - extract representative image frames
   - resize/re-encode frames according to config
   - send the frames to the selected ChatGPT model
   - request strict JSON output with exactly two VLM fields
   - validate the response JSON schema
   - if invalid, perform one JSON repair retry (or configurable number of retries)
   - record timing and token usage
   - append the result to the output structure
   - append a log entry
   - update console progress
6. Flush partial results to disk according to config.
7. At the end, write the final JSON output and final log summary.

---

## 5. Command-Line Interface

The script must use configuration files rather than many command-line arguments.

### 5.1 Required behavior

- The script accepts **one optional command-line parameter** specifying the configuration file path.
- The script reacts to `-h` and `--help` by explaining:
  - what the script does
  - that configuration is read from an INI file
  - the default config file name
- If no config path is provided, the script must look for:

```text
video_descr_generator.conf
```

in the current working directory.

### 5.2 Examples

```bash
python video_descr_generator.py
```

Uses:

```text
./video_descr_generator.conf
```

```bash
python video_descr_generator.py my_test_run.conf
```

Uses:

```text
./my_test_run.conf
```

```bash
python video_descr_generator.py /etc/video_descr_generator/chatgpt_run.conf
```

Uses the explicitly provided path.

### 5.3 Error behavior

- If the config file does not exist, the script must exit with a clear error message and a non-zero exit code.
- If the config file is invalid or missing required keys, the script must exit with a clear validation error and a non-zero exit code.
- If startup validation fails (missing `ffmpeg`, missing API key env var, invalid output path policy, etc.), the script must exit before scanning files.

---

## 6. Configuration File

The script must read all runtime settings from a configuration file.

### 6.1 Preferred format

Use **INI format** for v1.

The default `video_descr_generator.conf` should include concise inline comments describing each configuration option, so the file is usable both as a runnable default and as example documentation.

### 6.2 Required configuration domains

The configuration must support at least:

- input directory
- output JSON file path
- log file path
- API/backend selection
- model-specific parameters
- prompt specification
- frame extraction settings
- image preprocessing settings
- behavior flags

### 6.3 Minimum required configuration fields

#### `[general]`

- `input_dir` — root directory to scan recursively
- `output_json` — path to JSON output file
- `log_file` — path to processing log file

#### `[prompting]`

- `prompt` — prompt used for requesting video description from the model

#### `[video_sampling]`

- `sample_interval_sec` — spacing between sampled frames in seconds for short videos
- `max_frames_per_video` — maximum number of frames to submit for one video
- `anchor_start_frames` — number of anchor frames reserved near the beginning for long videos; for v1 use `2`
- `anchor_end_frames` — number of anchor frames reserved near the end for long videos; for v1 use `2`

#### `[preprocessing]`

- `max_dimension` — maximum width or height in pixels while preserving aspect ratio
- `jpeg_quality` — JPEG quality used when re-encoding resized images
- `detail` — image detail mode passed to the model (`low`, `high`, `auto`, or `original`)

#### `[chatgpt]`

- `api_key_env` — name of environment variable holding API key
- `model` — model name
- `timeout_sec` — request timeout
- `max_output_tokens` — upper bound for model response tokens
- `api_retry_count` — retry count for API transport failures
- `api_retry_initial_delay_sec` — initial API retry delay; doubled after each retry
- `api_retry_max_delay_sec` — maximum delay between API retries

#### `[behavior]`

- `overwrite_output` — whether existing output files may be overwritten
- `skip_if_already_present` — whether to skip files already present in output JSON
- `skip_statuses` — comma-separated statuses eligible for skipping; default `ok`
- `flush_every_n` — how often to flush partial results to disk
- `json_retry_count` — number of retries for invalid JSON; v1 default is `1`
- `continue_on_error` — whether processing continues after a per-file failure
- `item_errors_for_termination` — stop the run early after this many `item_error` records
- `max_num_of_files_processed` — process at most this many non-skipped files in one run; `0` means unlimited
- `parallel_threads` — number of video files processed concurrently, including extraction, preprocessing, and API call
- `heartbeat_every_n` — write heartbeat log event after every N scanned files; `0` disables count-based heartbeat
- `heartbeat_interval_sec` — write heartbeat log event at least every N seconds; `0` disables time-based heartbeat
- `use_lock_file` — whether to create an output lock file to prevent concurrent writers
- `backup_existing_output` — whether to create a timestamped `.bak` copy of an existing output JSON before overwrite

### 6.4 Important design constraints

- All behavior that a user may reasonably want to tune must be in config.
- The prompt must be user-configurable.
- The config must be sufficient to reproduce a run.
- The implementation must use only images extracted from the video; audio is always ignored.

### 6.5 Example config

Instruction to Codex agent: use the following sample configuration (including comments) as the basis for generating `video_descr_generator.conf` file during implementation.

```ini
[general]
# Root directory scanned recursively for .avi, .mov, .m2ts, .mts, .mp4, .3gp, and .m4v files. Relative paths are resolved from this config file.
input_dir = ./sample_videos

# Output JSON containing one record per video. Relative paths are resolved from this config file.
output_json = ./video_descriptions.json

# Line-oriented processing log. Relative paths are resolved from this config file.
log_file = ./video_descr_generator.log

[prompting]
# Prompt sent to the model together with sampled video frames.
# The current code expects exactly these two output field names: headline_cs and description_cs.
# The original project used Czech text; edit this prompt if you want another language.
prompt = Jsi asistent pro popis videí. Dostaneš několik obrazových snímků vybraných z jednoho videa. Popisuj pouze to, co je viditelné na snímcích. Vrať pouze jeden JSON objekt bez markdownu a bez dalších komentářů. JSON musí mít přesně dvě pole: "headline_cs" a "description_cs". Pole "headline_cs" musí být krátký český titulek o délce přibližně 4 až 10 slov. Pole "description_cs" musí být souvislý popis v češtině o 1 až 4 větách, který popíše osoby, jejich činnosti a prostředí viditelné ve videu. Pokud si nejsi jistý, nepřidávej spekulace a použij opatrnou formulaci.

[video_sampling]
# For short videos, sample one frame every N seconds, subject to the max frame cap.
sample_interval_sec = 2.0

# Hard cap for number of frames submitted for one video.
max_frames_per_video = 12

# For long videos, reserve this many anchor frames near the beginning.
anchor_start_frames = 2

# For long videos, reserve this many anchor frames near the end.
anchor_end_frames = 2

[preprocessing]
# Resize so the longer side is at most this value. Never upscale.
max_dimension = 512

# JPEG quality used for in-memory re-encoding before upload.
jpeg_quality = 85

# Lower detail saves tokens and is the recommended default for the PoC.
detail = low

[chatgpt]
# Environment variable that stores the OpenAI API key.
api_key_env = OPENAI_API_KEY

# Vision-capable model used for frame analysis.
model = gpt-5.4

# Timeout for one API request in seconds.
timeout_sec = 300

# Maximum response tokens requested from the model.
max_output_tokens = 800

# Retry count for API transport failures such as timeouts, connection errors, rate limits, or transient server errors.
api_retry_count = 5

# Initial delay before retrying an API transport failure. Delay doubles after each retry.
api_retry_initial_delay_sec = 5

# Maximum delay between API transport retries.
api_retry_max_delay_sec = 120

[behavior]
# If true, existing output_json and log_file may be overwritten.
overwrite_output = true

# If true and overwrite_output=true, copy an existing output_json to a timestamped .bak file before the run.
backup_existing_output = true

# If true, load the existing output JSON and skip already processed relative_path entries.
skip_if_already_present = true

# Comma-separated statuses eligible for skipping when skip_if_already_present=true.
# Use ok to retry previous error records on the next run. Use ok,error to skip all existing records.
skip_statuses = ok

# Flush partial output JSON after every N newly processed videos.
flush_every_n = 5

# Retry count used only when the model returns invalid JSON or missing required fields.
json_retry_count = 1

# Continue processing remaining videos after a per-file failure.
continue_on_error = true

# Terminate the run early after this many item_error records.
item_errors_for_termination = 10

# Process at most this many non-skipped files in one run. Use 0 for unlimited.
max_num_of_files_processed = 0

# Number of video files processed concurrently, including frame extraction, preprocessing, and API call.
parallel_threads = 3

# Write heartbeat log events after every N scanned files. Use 0 to disable count-based heartbeat.
heartbeat_every_n = 25

# Write heartbeat log events at least every N seconds during active processing. Use 0 to disable time-based heartbeat.
heartbeat_interval_sec = 300

# If true, create output_json.lock to prevent two runs writing the same output file.
use_lock_file = true
```

---

## 7. Video Sampling and Image Preprocessing

This chapter is critical because the VLM does not consume raw video directly in this solution.

### 7.1 Supported input formats

The script must process files with extensions:

- `.avi`
- `.mov`
- `.m2ts`
- `.mts`
- `.mp4`
- `.3gp`
- `.m4v`

Extension matching should be case-insensitive.

The `.mpg` extension is intentionally not included in the supported set for now.

### 7.2 Required external tools

The implementation must use:

- `ffprobe` to read duration and metadata where possible
- `ffmpeg` to extract representative frames

The script may assume both tools are available on `PATH` for v1.

### 7.3 Frame sampling strategy

For v1, use the following **adaptive duration-aware sampling strategy**.

Let:

- `D` = video duration in seconds
- `sample_interval_sec` = configured short-video sampling interval
- `max_frames_per_video` = configured frame cap
- `anchor_start_frames` = number of beginning anchor frames for long videos
- `anchor_end_frames` = number of ending anchor frames for long videos

Required behavior:

- If `D <= sample_interval_sec * max_frames_per_video`, use **fixed-interval sampling**.
  - Sample frames at approximately every `sample_interval_sec` seconds.
  - Do not exceed `max_frames_per_video`.
- Else, treat the video as a **long video** and sample using a hybrid coverage strategy:
  - reserve `anchor_start_frames` frames near the beginning
  - reserve `anchor_end_frames` frames near the end
  - distribute the remaining frames uniformly across the middle of the video
- For v1, the default long-video policy is:
  - `anchor_start_frames = 2`
  - `anchor_end_frames = 2`
  - remaining frames = `max_frames_per_video - anchor_start_frames - anchor_end_frames`
- The implementation must avoid concentrating all samples at the beginning of long videos.
- The implementation must avoid duplicate or near-duplicate timestamps when possible.
- For very short videos, submit at least one frame.
- The exact timestamps sent for each video must be stored in the output JSON.

Implementation guidance for long videos:

- Beginning anchor frames should be selected near the start of the clip, rather than both at timestamp 0.
- Ending anchor frames should be selected near the end of the clip, rather than both at the exact last frame.
- Ending timestamps should include a safety margin before the container end to avoid fragile near-EOF extraction failures on formats such as MTS/M2TS.
- The middle frames should cover the time span between the beginning-anchor region and the ending-anchor region as uniformly as practical.
- If the configured anchor counts would leave no room for middle frames, the implementation should still respect `max_frames_per_video` and choose the most reasonable spread across the full duration.

### 7.4 Frame extraction behavior

- Extract frames to memory where practical.
- For videos where `D <= sample_interval_sec * max_frames_per_video`, the implementation should extract all fixed-interval frames with one `ffmpeg` process.
  - The batch extraction should use a timestamp-aware `select` filter based on the exact timestamps computed by the script.
  - The batch extraction must return the same number of frames as the timestamp list.
  - If batch extraction fails or returns an unexpected number of frames, the implementation should fall back to per-timestamp extraction for that video.
- For long videos, per-timestamp extraction remains acceptable because timestamps are sparse and intentionally distributed across the beginning, middle, and end.
- The implementation may pipe PNG images from `ffmpeg` and then re-encode them with Pillow; the images sent to the VLM must still be JPEGs after preprocessing.
- Temporary files are acceptable if implementation simplicity requires them, but they must be cleaned up.
- If frame extraction fails, mark the video item as `status = error` and log the failure.

### 7.5 Image preprocessing behavior

- Resize each frame so its longer side is at most `max_dimension`.
- Preserve aspect ratio.
- Never upscale images.
- Re-encode frames as JPEG in memory using `jpeg_quality`.
- Pass the configured `detail` value on every image sent to the OpenAI API.

### 7.6 Recommended defaults for the PoC

The recommended default settings are:

- `sample_interval_sec = 2.0`
- `max_frames_per_video = 12`
- `anchor_start_frames = 2`
- `anchor_end_frames = 2`
- `max_dimension = 512`
- `jpeg_quality = 85`
- `detail = low`

With these defaults:

- short videos use dense fixed-interval sampling
- long videos use 2 beginning anchors, 2 ending anchors, and the remaining 8 frames spread across the middle

These settings are intended to balance descriptive usefulness, whole-video coverage, and API cost.

---

## 8. OpenAI / ChatGPT Backend Requirements

### 8.1 API behavior

The script must use an OpenAI ChatGPT vision-capable model through the official Python SDK.

Required behavior:

- Build one request per video.
- The request must include:
  - one text instruction from `[prompting]`
  - multiple input images (the sampled frames)
- The output must be requested as JSON and then validated locally.

### 8.2 Output schema required from the model

The model must return exactly this logical structure:

```json
{
  "headline_cs": "Krátký český titulek",
  "description_cs": "Český popis osob, činností a prostředí viditelných na snímcích."
}
```

Requirements:

- Response language must be Czech.
- No markdown.
- No prose outside JSON.
- No extra top-level fields.
- If uncertainty exists, the wording should stay cautious and non-speculative.

### 8.3 Retry behavior

API transport failures such as timeouts, connection errors, rate limits, and transient server errors must be retried up to `api_retry_count` times with exponential backoff controlled by `api_retry_initial_delay_sec` and `api_retry_max_delay_sec`. Rate-limit failures must be logged with `api_rate_limit` instead of the generic `api_retry` event. If all API attempts fail, the script must log `api_retries_exhausted` or `api_rate_limit_exhausted` before converting the video to an `item_error` record.

If the first model response is invalid because:

- it is not parseable JSON, or
- one of the required fields is missing, or
- one of the required fields is empty,

then the script must retry, up to `json_retry_count` times.

The retry prompt should clearly state that the previous answer was invalid and that the model must return only a valid JSON object with the required two fields.

### 8.4 Token accounting

If the SDK returns usage data, record at least:

- `input_tokens`
- `output_tokens`
- `total_tokens` if available

If some value is not available from the SDK, record `null` and continue.

---

## 9. Output JSON File

The generated results must be saved to a JSON file suitable for downstream indexing or later embedding.

### 9.1 Required structure

Use a top-level JSON array, with one object per processed video.

The script must write UTF-8.
The script should write output JSON as UTF-8 with BOM (`utf-8-sig`) to improve Windows editor auto-detection for non-ASCII text, such as accented characters.
The script must read existing output JSON using UTF-8 with optional BOM support when `skip_if_already_present = true`.

- ### 9.2 Required fields per item


Each record must contain at least the following fields:

- `relative_path` — path relative to `input_dir`
- `file_name` — base file name
- `status` — `ok` or `error`
- `error_message` — `null` on success, otherwise a readable message
- `model` — model name from config
- `headline_cs` — VLM-generated short Czech headline on success, otherwise `null`
- `description_cs` — VLM-generated Czech description on success, otherwise `null`
- `attempt_count` — number of model attempts used for this video
- `json_validated` — boolean
- `elapsed_sec` — total processing time for this video in seconds
- `input_tokens` — integer or `null`
- `output_tokens` — integer or `null`
- `total_tokens` — integer or `null`
- `video_duration_sec` — numeric duration if available, otherwise `null`
- `frames_submitted` — number of frames sent to the model
- `frame_timestamps_sec` — list of timestamps used for the frames sent to the model

### 9.3 Required semantics

- `headline_cs` and `description_cs` are the **only two text fields produced by the VLM**.
- All other fields are metadata produced by the script.
- On `status = error`, both `headline_cs` and `description_cs` must be `null`.
- `error_message` must be brief but actionable.

### 9.4 Example successful item

```json
{
  "relative_path": "trip/day1/clip_0007.mov",
  "file_name": "clip_0007.mov",
  "status": "ok",
  "error_message": null,
  "model": "gpt-4.1-mini",
  "headline_cs": "Rodina na pláži u moře",
  "description_cs": "Na videu jsou vidět lidé na pláži poblíž moře. Několik osob se pohybuje po písku a tráví čas venku. Prostředí působí jako pobřežní rekreační místo za denního světla.",
  "attempt_count": 1,
  "json_validated": true,
  "elapsed_sec": 5.42,
  "input_tokens": 1180,
  "output_tokens": 65,
  "total_tokens": 1245,
  "video_duration_sec": 23.8,
  "frames_submitted": 10,
  "frame_timestamps_sec": [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 23.5]
}
```

### 9.5 Example failed item

```json
{
  "relative_path": "damaged/broken_clip.m2ts",
  "file_name": "broken_clip.m2ts",
  "status": "error",
  "error_message": "ffmpeg frame extraction failed",
  "model": "gpt-4.1-mini",
  "headline_cs": null,
  "description_cs": null,
  "attempt_count": 0,
  "json_validated": false,
  "elapsed_sec": 0.73,
  "input_tokens": null,
  "output_tokens": null,
  "total_tokens": null,
  "video_duration_sec": null,
  "frames_submitted": 0,
  "frame_timestamps_sec": []
}
```

---

## 10. Log File

The script must produce a line-oriented text log file designed for both human reading and troubleshooting.

### 10.1 Log goals

The log must make it possible to understand:

- how the run was configured
- what the script scanned
- what happened for each video
- where failures occurred
- whether retries happened
- how expensive the run was in time and tokens

### 10.2 Log format requirements

Use one line per event with this structure:

```text
TIMESTAMP | LEVEL | EVENT | key=value | key=value | ...
```

Where:

- `TIMESTAMP` is local date and time in `YYYY-MM-DD HH:MM:SS`
- `LEVEL` is one of `START`, `INFO`, `ITEM`, `WARN`, `ERROR`, `SUMMARY`
- `EVENT` is a short event identifier
- remaining fields are `key=value` pairs

### 10.3 Events that must be logged

At minimum, log these events:

- `run_start`
- `config_loaded`
- `scan_complete`
- `item_start`
- `api_retry`
- `api_rate_limit`
- `api_retries_exhausted`
- `api_rate_limit_exhausted`
- `item_retry`
- `item_done`
- `item_error`
- `item_error_threshold_reached`
- `max_num_of_files_processed_reached`
- `flush_output`
- `heartbeat`
- `backup_output`
- `config_snapshot`
- `run_summary`
- `run_end`

### 10.4 Per-item fields

For successful or failed item completion events, log at least:

- `index`
- `total`
- `path`
- `status`
- `attempts`
- `json_validated`
- `elapsed_sec`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `frames_submitted`
- `retry_reason` when applicable
- `error` when applicable

### 10.5 Example log excerpt

```text
2026-04-22 19:10:03 | START   | run_start     | backend=chatgpt | model=gpt-4.1-mini | input_dir=/mnt/videos/testset | output_json=/mnt/videos/descriptions.json | log_file=/mnt/videos/video_descr_generator.log
2026-04-22 19:10:03 | INFO    | config_loaded | sample_interval_sec=2.0 | max_frames_per_video=12 | anchor_start_frames=2 | anchor_end_frames=2 | max_dimension=512 | jpeg_quality=85 | detail=low | json_retry_count=1 | skip_if_already_present=false
2026-04-22 19:10:04 | INFO    | scan_complete | files_found=1520

2026-04-22 19:10:05 | INFO    | item_start    | index=1 | total=1520 | path=trip/day1/clip_0001.mov
2026-04-22 19:10:09 | ITEM    | item_done     | index=1 | total=1520 | path=trip/day1/clip_0001.mov | status=ok | attempts=1 | json_validated=true | frames_submitted=10 | elapsed_sec=4.91 | input_tokens=1234 | output_tokens=74 | total_tokens=1308

2026-04-22 19:10:10 | INFO    | item_start    | index=2 | total=1520 | path=trip/day1/clip_0002.mov
2026-04-22 19:10:14 | WARN    | item_retry    | index=2 | total=1520 | path=trip/day1/clip_0002.mov | retry_no=1 | retry_reason=invalid_json
2026-04-22 19:10:16 | ITEM    | item_done     | index=2 | total=1520 | path=trip/day1/clip_0002.mov | status=ok | attempts=2 | json_validated=true | frames_submitted=10 | elapsed_sec=6.37 | input_tokens=1288 | output_tokens=80 | total_tokens=1368

2026-04-22 19:10:17 | INFO    | item_start    | index=3 | total=1520 | path=damaged/broken_clip.m2ts
2026-04-22 19:10:17 | ERROR   | item_error    | index=3 | total=1520 | path=damaged/broken_clip.m2ts | status=error | attempts=0 | json_validated=false | frames_submitted=0 | elapsed_sec=0.73 | error=ffmpeg frame extraction failed

2026-04-22 19:10:17 | INFO    | flush_output  | processed_since_last_flush=3 | output_json=/mnt/videos/descriptions.json

2026-04-22 20:43:11 | SUMMARY | run_summary   | files_found=1520 | ok=1512 | failed=8 | retried=97 | total_elapsed_sec=5587.20 | avg_elapsed_sec=3.68
2026-04-22 20:43:11 | SUMMARY | run_summary   | total_input_tokens=1823400 | total_output_tokens=103220 | total_tokens=1926620 | avg_input_tokens=1205.95 | avg_output_tokens=68.27 | avg_total_tokens=1274.22
2026-04-22 20:43:11 | SUMMARY | run_end       | status=completed
```

### 10.6 Logging quality requirements

- Logging must be append-safe for the duration of the run.
- Log messages must not contain stack traces unless the run is aborted; concise error summaries are preferred for item-level failures.
- Unexpected exceptions should be logged with enough context to locate the affected video.

---

## 11. Console Progress Output

The script must show progress on console output.

### 11.1 Minimum progress requirements

The user must be able to see:

- number of files found during initial scan
- current progress in a form such as:
  - `Processing 37/1520: subdir1/clip_0042.mov`
- running percentage complete
- elapsed total runtime

### 11.2 Nice-to-have

- current file processing time after each file
- current retry count for the file when applicable
- counts of `ok`, `error`, and `skipped`

A simple text progress format is sufficient; no GUI is required.

---

### 11.3 Recommended Unattended Run Command

For long production runs where the user may disconnect from the terminal, the recommended invocation is:

```bash
nohup python3 -u video_descr_generator.py production.conf > production.out 2>&1 &
```

Rationale:

- `nohup` keeps the process running after terminal disconnect.
- `python3 -u` makes stdout and stderr unbuffered, so progress is visible promptly in `production.out`.
- `> production.out 2>&1` captures console progress and unexpected stderr output in one file.
- The trailing `&` runs the process in the background.

Useful monitoring commands:

```bash
tail -f production.out
tail -f video_descr_generator.log
```

On WSL, prefer following the log file by name because files under `/mnt/c` can behave differently from native Linux filesystems:

```bash
tail -F video_descr_generator.log
```

On native Linux filesystems, `tail -f video_descr_generator.log` is sufficient.

The log file remains the primary source for structured progress, token use, retries, failures, and final summary.

---

## 11.4 Debug Frame Export

The repository includes an optional debug entry point:

```bash
python video_descr_generator_debug.py
```

Required behavior:

- It must read the same INI config format and accept the same optional config path argument as the main script.
- It must use the same frame sampling, `ffmpeg` extraction, Pillow preprocessing, OpenAI request, validation, logging, output JSON, and progress behavior as the main script.
- Immediately before the OpenAI request, it must save the exact JPEG byte payloads that will be sent as VLM image inputs.
- The default debug output directory is:

```text
debug_vlm_frames/
```

- The debug output directory may be overridden with:

```text
VIDEO_DESCR_DEBUG_FRAMES_DIR
```

- For each processed video, the debug script must write a separate directory containing:
  - `frame_001.jpg`, `frame_002.jpg`, etc.
  - `manifest.json` with at least relative video path, saved timestamp, frame file names, byte counts, and SHA-256 hashes
- If `skip_if_already_present = true` causes a video to be skipped, no debug frames are written for that skipped video.
- Debug frame output must be ignored by Git by default.

---

## 12. Error Handling and Restart Behavior

### 12.1 Per-file failures

The script must handle these categories gracefully:

- unreadable or corrupt video file
- `ffprobe` failure
- `ffmpeg` frame extraction failure
- image preprocessing failure
- API timeout or transport error
- model response not valid JSON
- schema validation failure

A failure for one video must not abort the whole run if `continue_on_error = true`.

Dependency startup errors must be printed clearly to the console and must terminate with a non-zero exit code before scanning or processing files starts.

For dependency install hints:

- On Windows and other non-Linux platforms, show `python -m pip install ...` style instructions.
- On Linux, show Debian/Ubuntu `apt install` style instructions, for example:
  - Pillow: `sudo apt install python3-pil`
  - OpenAI SDK: `sudo apt install python3-openai`

---

### 12.2 Restart / skip behavior

If `skip_if_already_present = true` and `output_json` already exists:

- load existing JSON
- index already processed items by `relative_path` and `status`
- skip only existing records whose status is listed in `skip_statuses`
- with default `skip_statuses = ok`, retry previous `status = error` records on the next run
- when a previous non-skipped record is retried, replace that prior record in the final output instead of appending a duplicate
- preserve prior skipped results unless `overwrite_output = true` and the user intentionally starts fresh

If `max_num_of_files_processed > 0`, the limit applies only to non-skipped files processed in the current run. For example, with 1,000 scanned files and `max_num_of_files_processed = 500`, the first run processes the first 500 files in sorted order. A second run with `skip_if_already_present = true` skips those existing successful records and processes the next 500 files.

### 12.3 Output flushing

When `flush_every_n` is reached:

- write the current JSON output to disk
- log a `flush_output` event
- continue processing

This reduces data loss if the run is interrupted.

### 12.4 Long unattended runs

For long runs, the script should:

- create an advisory `output_json.lock` file when `use_lock_file = true`
- refuse to start if the lock file belongs to a running process
- remove stale lock files when the recorded process is no longer running
- create a timestamped `.bak` copy of an existing `output_json` when `backup_existing_output = true` and `overwrite_output = true`
- handle `SIGINT`, `SIGTERM`, and `SIGHUP` by requesting shutdown, flushing current results, logging `run_end`, and exiting after the current item
- print progress with flushing so `nohup python3 -u ...` output is timely
- log heartbeat events by count or elapsed time according to `heartbeat_every_n` and `heartbeat_interval_sec`
- terminate the run early when the cumulative number of `item_error` records reaches `item_errors_for_termination`
- stop after `max_num_of_files_processed` non-skipped files and log `max_num_of_files_processed_reached`; this configured cap is considered a successful partial run
- process up to `parallel_threads` video files concurrently while preserving sorted output order, central counters, flush behavior, and termination checks

---

## 13. Quality Requirements

### 13.1 Maintainability

The code should be structured so backend logic, video sampling, and orchestration are clearly separated.

Suggested internal components (inside one script or helper modules):

- config loading and validation
- directory scanning
- video metadata reader
- frame sampling and extraction
- image preprocessing
- OpenAI backend client
- response parsing and schema validation
- output writer
- logger
- main orchestration

### 13.2 Readability

- Use clear function names.
- Add docstrings.
- Add comments where API integration or ffmpeg handling is non-obvious.

### 13.3 Determinism

- Files must be processed in deterministic sorted order.
- Frame timestamp selection must be deterministic for the same input and config.
- Config must fully determine behavior, aside from model nondeterminism.

### 13.4 Robustness

- Handle corrupt or unsupported videos gracefully.
- Handle timeouts and transient API failures with clear logging.
- Handle invalid JSON robustly.
- Avoid crashing due to one bad file.

### 13.5 Performance

This is a proof of concept, not final production code.

For v1:

- Single-process, sequential execution is acceptable.
- Correctness, observability, schema stability, and reproducibility matter more than maximum throughput.

Parallelization may be added later, but it is not required in this PRD.

---

## 14. Technical Design Guidance for Codex

### 14.1 Language and runtime

- Target Python version: **3.12.3**
- Target execution environment: **Linux**, but the script should remain Windows-compatible if paths and external tools are adjusted

### 14.2 Recommended dependencies

Likely dependencies include:

- `openai`
- `Pillow`
- standard library modules such as `json`, `configparser`, `pathlib`, `subprocess`, `time`, `logging`, `base64`, `io`

Optional but recommended:

- `pydantic` or `jsonschema` for schema validation

### 14.3 Implementation recommendations

- Use `pathlib` for path handling.
- Use `configparser` for INI parsing.
- Use `subprocess` to call `ffprobe` and `ffmpeg`.
- Prefer extracting lossless still images from `ffmpeg` into memory and then performing the final JPEG resize/re-encode with Pillow. This avoids MJPEG encoder compatibility problems seen with some MTS/M2TS sources.
- Use one `ffmpeg` process for short fixed-interval videos where practical. The simple `fps=1/N` filter is not precise enough for the existing near-end timestamp behavior, so use a generated `select` expression that chooses the first frame at or after each computed timestamp.
- Keep the per-timestamp `ffmpeg` path as a fallback and for long videos.
- Use in-memory JPEGs when sending images to the API.
- Validate model output before writing it to the final JSON.
- Keep JSON writing atomic where practical (write temp file then replace).
- Ignore local generated/debug artifacts in Git, including Python bytecode caches, `.bak` files, `debug_vlm_frames/`, `test_videos/`, `video_descr_generator.log`, and `video_descriptions.json`.

### 14.4 Suggested processing flow per video

1. Determine relative path.
2. Read duration via `ffprobe`.
3. Compute frame timestamps.
4. Extract frames.
5. Resize/re-encode frames.
6. Call ChatGPT with the configured prompt and all frames.
7. Parse and validate JSON.
8. Retry if required.
9. Record metrics.
10. Append result.

---

## 15. Acceptance Criteria

The implementation is acceptable when all of the following are true:

1. Running the script with a valid config scans the input directory recursively for `.avi`, `.mov`, `.m2ts`, `.mts`, `.mp4`, `.3gp`, and `.m4v` files.
2. The script extracts frames from each video and sends only images to the model.
3. Audio is ignored.
4. The model output stored in JSON contains exactly two VLM-generated text fields:
   - `headline_cs`
   - `description_cs`
5. Both text fields are in Czech language on successful items.
6. The output JSON contains the required metadata fields and per-item status.
7. Invalid JSON responses trigger retry behavior according to config.
8. The log file contains startup, item, retry, error, flush, and summary events.
9. The console shows progress throughout the run.
10. The script continues past individual file failures when `continue_on_error = true`.
11. The debug entry point saves the exact JPEG images sent to the VLM request, plus a manifest, without changing the main script's normal output behavior.
12. Short fixed-interval videos can be extracted with one `ffmpeg` batch call, while preserving fallback behavior for extraction edge cases.
