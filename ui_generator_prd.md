# Product Requirements Document: `ui_generator.py`

## 1. Purpose

After running `video_descr_generator.py`, the project has two production JSON outputs for two distinct video archives:

- SD archive: `D:\VideoLibrary\PrimaryArchive\`
- HD archive: `D:\VideoLibrary\SecondaryArchive\`

`ui_generator.py` creates one portable Windows Electron application that browses both archives together, displays the VLM-generated `headline_cs` and `description_cs` fields, groups clips by calendar day, provides offline smart search, and opens videos in the default Windows application.

The generator reads a configuration file, preprocesses the production JSON data, creates thumbnails, creates or reuses day summaries, creates or reuses semantic search embeddings, and writes the complete portable UI bundle into the primary archive under `__UI_data__`.

## 2. Current Implementation Status

Implemented files:

```text
ui_generator.py
ui_generator.conf
```

Current production output folder:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\
```

The generated app has been verified to launch from:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\run_ui.bat
```

The app also includes a no-console launcher:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\run_ui.vbs
```

## 3. Agreed System Architecture

### 3.1 One Combined App

The UI is one combined app for both archives, not one app per archive.

The SD archive is the primary archive:

```ini
primary_archive_root = D:\VideoLibrary\PrimaryArchive\
```

The HD archive is the secondary archive:

```ini
secondary_archive_root = D:\VideoLibrary\SecondaryArchive\
```

The generator creates and maintains:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\
```

All UI files, preprocessed metadata, thumbnails, day summaries, search data, Electron dependencies, and model files are stored under this folder.

### 3.2 Portable Relative Paths

The generated UI does not depend on the absolute paths used on the generator PC.

At runtime, the app locates the primary archive root as the parent folder of `__UI_data__`:

```text
D:\VideoLibrary\PrimaryArchive\
```

Each archive root is resolved by a path relative to that primary archive root:

```json
[
  {
    "archive_id": "SD",
    "label": "SD videa",
    "relative_root": "."
  },
  {
    "archive_id": "HD",
    "label": "HD videa",
    "relative_root": "..\\Video.HD"
  }
]
```

Example runtime path for an HD video:

```text
..\Video.HD\100605\20100509122029.m2ts
```

If both archives are copied to another drive, USB disk, or mapped network drive, the app continues to work as long as the relative relationship is preserved:

```text
X:\DV Capture (stará videokamera)\__UI_data__\
X:\Video.HD\
```

The production JSON outputs from `video_descr_generator.py` contain Linux-style relative paths using `/`. `ui_generator.py` normalizes them to Windows-style paths using `\` in generated UI data.

### 3.3 Generated Folder Structure

The generated folder structure is:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\
  run_ui.bat
  run_ui.vbs
  run_ui_debug.bat
  app\
    package.json
    package-lock.json
    node_modules\
      electron\
        dist\
          electron.exe
      @huggingface\
      ...
    main.js
    preload.js
    run_ui.bat
    run_ui.vbs
    run_ui_debug.bat
    embedding\
      transformers_embedding.mjs
    renderer\
      index.html
      app.js
      styles.css
    transformers_cache\
      intfloat\
        multilingual-e5-small\
          config.json
          tokenizer.json
          tokenizer_config.json
          onnx\
            model.onnx
  data\
    library_manifest.json
    videos.json
    days.json
    embeddings_manifest.json
    embeddings.f32
  thumbnails\
    <video_id>.jpg
  cache\
    day_summary_cache.json
    thumbnail_manifest.json
    embedding_manifest.json
    embedding_work\
```

The UI app loads generated metadata and the binary embedding file from local disk at startup. No database server, local web server, Python process, Node.js installation, or API key is required on viewing PCs.

## 4. Goals and Non-Goals

### 4.1 Primary Goals

- Build one generated Windows UI app for both SD and HD archives.
- Browse videos by calendar date.
- Display VLM-generated `headline_cs` and `description_cs`.
- Display generated thumbnails.
- Show clip duration under each thumbnail with label `délka`.
- Open a video in the default Windows application by clicking its thumbnail or the open button.
- Open video folders in Windows Explorer.
- Provide offline smart search across descriptions, headlines, filenames, folder names, archive labels, and paths.
- Keep the app portable across drive letters, USB drives, and mapped network drives.
- Avoid server processes and external database services.

### 4.2 Non-Goals

- The UI is not a video player. It launches videos in the default Windows application.
- The runtime UI does not call GPT APIs.
- The runtime UI does not regenerate thumbnails, summaries, or embeddings.
- The runtime UI does not modify the production JSON outputs from `video_descr_generator.py`.
- Remote/viewing PCs do not run `ui_generator.py`.

## 5. `ui_generator.py`

### 5.1 Runtime

- Target Python version: `3.12.3`
- Target execution environment: Windows
- Normal execution reads all settings from a configuration file.
- Default config path: `ui_generator.conf`
- Node.js and npm are required on the generator PC.
- ffmpeg is required on the generator PC for thumbnail generation.
- OpenAI API access is required only when `mock_gpt_api = false` and uncached real summaries must be generated.
- Remote/viewing PCs must not need Python, Node.js, npm, ffmpeg, OpenAI API access, or network access.

### 5.2 Implemented Command-Line Interface

Normal use:

```bat
cd C:\projects\video-library-smart-search
python -X utf8 ui_generator.py ui_generator.conf
```

Optional development flags:

```text
--limit N             Generate only the first N sorted records for a smoke test.
--skip-npm-install    Write app files but do not install or verify app dependencies.
--skip-embeddings     Skip embedding generation even when enabled in config.
--skip-thumbnails     Skip thumbnail generation even when enabled in config.
```

These flags are for development only. Production generation should run without them.

### 5.3 Current Config Shape

Current production config:

```ini
[general]
; Archive id used as the path baseline for runtime video paths.
primary_archive_id = SD
; Output folder for generated data, cache, and the Electron UI app.
ui_output_dir = D:\VideoLibrary\PrimaryArchive\__UI_data__
; When true, rewrites the generated Electron app files on each run.
overwrite_ui_app = true

[archive:SD]
; Human-readable archive name shown in the UI and search text.
label = SD videa
; Root folder where this archive's video files live.
archive_root = D:\VideoLibrary\PrimaryArchive
; Description generator JSON file for this archive.
input_json = C:\projects\video-library-smart-search\sample_data\primary_video_descriptions.json

[archive:HD]
; Human-readable archive name shown in the UI and search text.
label = HD videa
; Root folder where this archive's video files live.
archive_root = D:\VideoLibrary\SecondaryArchive
; Description generator JSON file for this archive.
input_json = C:\projects\video-library-smart-search\sample_data\secondary_video_descriptions.json

[thumbnails]
; Enables thumbnail generation for usable video records.
enabled = true
; When true, keeps existing thumbnail JPEGs whose source video still exists.
skip_existing_thumbnails = true
; Generated thumbnail width in pixels.
width = 240
; Generated thumbnail height in pixels.
height = 135
; JPEG quality for generated thumbnails, from 1 to 100.
jpeg_quality = 85

[summaries]
; Enables per-day summary generation.
enabled = true
; When true, uses deterministic mock summaries instead of calling the OpenAI API.
mock_gpt_api = false
; OpenAI model used for real per-day summaries.
model = gpt-5.5
; Environment variable that contains the OpenAI API key.
api_key_env = OPENAI_API_KEY
; When true, reuses existing real summaries whose input hash still matches.
skip_existing_day_summaries = true

[search]
; Enables local semantic search in the generated UI.
enabled = true
; When true, reuses embeddings only if the whole embedding cache is current:
; embeddings.f32 and embeddings_manifest.json exist, and cache/embedding_manifest.json
; has a source_hash matching the current model, dtype, runtime, prefix, video ids,
; and embedding text. Any changed description, added or removed video, changed
; path/archive label, or changed embedding setting regenerates the full embedding set.
skip_existing_embeddings = true
; Transformers.js embedding model used for video search text.
embedding_model = intfloat/multilingual-e5-small
; Embedding runtime implementation; currently only transformers_js is supported.
embedding_runtime = transformers_js
; Numeric dtype requested from the embedding runtime.
embedding_dtype = fp32
; Number of records embedded per helper batch. This controls how much embedding
; work is processed in parallel, so set it according to the CPU available on
; the system.
embedding_batch_size = 12
; App-local cache folder for downloaded embedding model files.
embedding_cache_dir = transformers_cache

[app]
; When true, installs or verifies npm/Electron dependencies for the generated app.
install_dependencies = true
; Node.js executable used for dependency setup and embedding helper scripts.
node_command = node
; npm executable used to install generated app dependencies.
npm_command = npm
```

### 5.4 Console Progress

`ui_generator.py` prints progress for expensive stages:

- output destination
- loading each production JSON file
- loaded usable video count
- Electron app file generation
- npm/Electron dependency installation or reuse
- thumbnail generation and reuse counts
- per-day summary progress
- embedding generation or cache reuse
- final launch path

Day summaries print one line per day. Example:

```text
Summary progress 411/414: reused 2014-09-06 (63 videos)
```

The summary cache is written after each day, so interrupted real-summary runs can be restarted without losing completed days.

## 6. Metadata Model

### 6.1 Video IDs

Each video has a stable `video_id`:

```text
<archive_id>_<sha1-of-archive-id-and-normalized-relative-path>
```

The hash input uses normalized Windows-style relative paths and casefolding.

### 6.2 Generated Video Record

Each entry in `data\videos.json` contains:

```json
{
  "video_id": "SD_abcd1234",
  "archive_id": "SD",
  "archive_label": "SD videa",
  "relative_path": "04 20030921-20031108\\04 20031108-16.06.45.avi",
  "runtime_relative_path": ".\\04 20030921-20031108\\04 20031108-16.06.45.avi",
  "file_name": "04 20031108-16.06.45.avi",
  "folder_relative_path": "04 20030921-20031108",
  "runtime_folder_path": ".\\04 20030921-20031108",
  "capture_date": "2003-11-08",
  "capture_time": "16:06:45",
  "timestamp_source": "filename_yyyymmdd_time",
  "timestamp_confidence": "high",
  "duration_sec": 18.4,
  "duration_text": "00:18",
  "thumbnail_path": "thumbnails\\SD_abcd1234.jpg",
  "headline_cs": "...",
  "description_cs": "..."
}
```

For HD videos, `runtime_relative_path` points from the primary archive root to the HD archive:

```text
..\Video.HD\100605\20100509122029.m2ts
```

## 7. Date and Time Extraction

`ui_generator.py` infers `capture_date` and, when possible, `capture_time` from the normalized relative path.

The extraction algorithm checks patterns in this order:

1. Full compact timestamp in filename or path: `YYYYMMDDHHMMSS`
   - Example: `20100509122029.m2ts`
   - Result: `2010-05-09 12:20:29`
2. Timestamp with separated time: `YYYYMMDD-HH.MM.SS`
   - Example: `04 20031108-16.06.45.avi`
   - Result: `2003-11-08 16:06:45`
3. Folder date with underscores: `YYYY_MM_DD`
   - Example: `2003_02_02 doma`
   - Result: `2003-02-02`, time unknown
4. Folder date in Czech/European notation: `DD.MM.YYYY`
   - Example: `2.2.2014`
   - Result: `2014-02-02`, time unknown
5. Short folder date: `YYMMDD`
   - Example: `110528 od Tomáše`
   - Result: `2011-05-28`, time unknown
   - Only accepted if it forms a valid date.
6. Date-only compact form: `YYYYMMDD`
   - Example: `20140202 Elí 20 min SCIO test.m2ts`
   - Result: `2014-02-02`, time unknown

If no date can be inferred:

```json
{
  "capture_date": null,
  "capture_time": null,
  "timestamp_source": "unknown",
  "timestamp_confidence": "none"
}
```

### 7.1 Sorting

The date list is sorted by `capture_date`.

Within one date:

- videos with known `capture_time` are sorted chronologically
- videos without known `capture_time` are sorted by `file_name`
- known-time videos come before unknown-time videos

## 8. Day Summaries

`ui_generator.py` generates day summaries offline and stores them in `data\days.json`.

For each day, the generator collects:

- `video_id`
- archive ID
- file name
- `headline_cs`
- `description_cs`

When `mock_gpt_api = true`, the generator produces deterministic placeholder summaries and does not call the OpenAI API.

When `mock_gpt_api = false`, the generator calls the OpenAI Responses API using:

```ini
model = gpt-5.5
api_key_env = OPENAI_API_KEY
```

The prompt asks for a concise Czech day summary in one to three sentences.

### 8.1 Summary Caching

Summary cache file:

```text
__UI_data__\cache\day_summary_cache.json
```

Each cached day summary is keyed by day and stores:

- day input hash
- summary text
- summary mode: `mock` or `openai`
- `mock_gpt_api` boolean
- model identity, for example `gpt-5.5`
- update timestamp

The generator reuses a cached day summary only when all of these match:

- `skip_existing_day_summaries = true`
- day input hash is unchanged
- cached summary mode matches the current mode
- cached `mock_gpt_api` matches the current config
- cached model identity matches the current config
- cached summary text exists

This prevents mock summaries from being reused when switching to real GPT summaries.

The cache is written after every processed day. If a long real-summary run is interrupted, rerunning the generator reuses completed real summaries and continues from the remaining days.

The verified production cache currently contains:

```text
414 entries: openai / mock_gpt_api=false / gpt-5.5
```

A repeat production run verified:

```text
summaries generated=0, reused=414
```

### 8.2 API Retry Behavior

OpenAI summary calls retry transient failures with exponential backoff.

Retried status categories:

- `408`
- `409`
- `429`
- `500`
- `502`
- `503`
- `504`
- transient URL/network failures

Non-retryable API errors stop the run so the problem is visible.

## 9. Thumbnails

Thumbnails are generated offline using `ffmpeg`.

Requirements implemented:

- first usable video frame
- JPEG thumbnails under `__UI_data__\thumbnails\`
- stable filenames based on `video_id`
- preserved aspect ratio
- no upscaling beyond source dimensions
- `skip_existing_thumbnails = true` reuses valid existing thumbnails

Thumbnail cache:

```text
__UI_data__\cache\thumbnail_manifest.json
```

The thumbnail cache records:

- `video_id`
- archive ID
- source relative path
- source file size
- source modified timestamp
- thumbnail path
- status

If source size or modified timestamp changes, the thumbnail is regenerated.

## 10. Smart Search

The app supports smart search over:

- `headline_cs`
- `description_cs`
- file name
- folder relative path
- normalized relative path
- archive label

Implementation:

- `ui_generator.py` precomputes video embeddings.
- Embeddings are stored in `data\embeddings.f32`.
- Metadata is stored in `data\embeddings_manifest.json`.
- The UI computes query embeddings locally using the bundled Electron Node runtime.
- The UI combines semantic similarity with lexical boosts.

The generated UI does not require an API key for search.

### 10.1 Selected Embedding Model and Runtime

Model:

```text
intfloat/multilingual-e5-small
```

Runtime:

```text
Transformers.js
```

The generator and UI use the same JavaScript helper:

```text
__UI_data__\app\embedding\transformers_embedding.mjs
```

The generator invokes the helper through Node.js for video embeddings. The Electron UI imports the same helper at runtime for query embeddings.

E5 prefixes:

- Video description embeddings use `passage: <text>`.
- Search query embeddings use `query: <text>`.

Video embedding text includes:

- `headline_cs`
- `description_cs`
- file name
- folder path
- archive label
- normalized relative path

Transformers.js uses:

- feature extraction pipeline
- mean pooling
- normalized vectors
- `fp32`
- 384 dimensions

Because vectors are normalized, search uses dot product as cosine similarity.

### 10.2 Search Result Score Labels

Each search result card may show a scoring breakdown such as:

```text
Skóre 1.120 · sémantika 0.840 · text 1.000
```

This line is an internal ranking explanation, not a percentage or confidence value.

- `sémantika` is the semantic similarity between the search query embedding and the video's embedding text.
- `text` is the direct lexical match score across filename, folder path, archive label, headline, and description. It is capped at `1.000`.
- `Skóre` is the final ranking score used to sort search results.

When semantic embeddings are available, the UI computes the final score as:

```text
score = semantic + lexical * 0.28
```

For example:

```text
0.840 + 1.000 * 0.28 = 1.120
```

This means the result matched both conceptually and through direct text terms, so it appears higher in the sorted search results.

### 10.3 Offline Model Loading

Model files are bundled under:

```text
__UI_data__\app\transformers_cache\intfloat\multilingual-e5-small\
```

The shared helper sets both:

- `env.cacheDir`
- `env.localModelPath`

and passes:

- `cache_dir`
- `local_files_only = true` for offline query search

This ensures remote/viewing PCs load the model from the bundled `transformers_cache` folder, not from `node_modules` and not from the network.

## 11. Electron UI

Implementation style:

- Electron
- plain JavaScript
- no TypeScript
- no web server
- all data loaded from generated local JSON/binary files
- Electron main process opens files/folders through Windows shell APIs

The app provides:

- resizable and maximizable window
- light blue theme
- Back and Forward buttons
- date list screen
- day video list screen
- search screen
- archive filter: SD, HD, or both
- startup loading splash with `Nahrávám data`
- thumbnail click opens video
- button opens video
- button opens folder in Windows Explorer

### 11.1 Date List

The first screen shows days.

Each day row shows:

- date
- archive coverage
- number of videos
- Czech summary
- folder buttons

If videos for a day span multiple folders or archives, folder buttons allow opening each relevant location.

When the user opens a day and then uses Back, the date list restores the previous scroll position.

### 11.2 Video Cards

Video cards show:

- thumbnail
- duration under the thumbnail as `délka M:SS` or `délka MM:SS`
- archive badge
- capture time when known
- compact capture date plus time in search results, for example `31.01.2008 14:23`
- headline
- normalized relative path
- open-video button
- open-folder button
- full-width description block

The description block initially clamps long text. Clicking the description expands it to full text; clicking again collapses it. Keyboard activation with Enter or Space is also supported.

Day detail cards show only the capture time because the day page already establishes the date. Search result cards show both the compact date and time so results from different days can be compared without opening the day detail.

### 11.3 Responsive Grid

Day and search video grids use:

```css
grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
```

The day and search pages allow content up to `1920px` wide, so widening the window increases the number of visible card columns from 3 to 4 or 5 depending on available width.

## 12. Remote PC and Portable Use

The generated app works when the primary archive is:

- moved to another drive letter
- copied to a USB disk
- attached as an external disk
- exposed as a network share
- mapped as a network drive on a remote PC

Remote/viewing PCs must not need:

- Python
- Node.js installed separately
- npm
- `npm install`
- ffmpeg
- OpenAI API key
- network access for search
- local server or database

Electron dependencies and the Electron runtime executable are bundled into:

```text
__UI_data__\app\node_modules\
```

The app includes three launchers:

- `__UI_data__\run_ui.bat` starts Electron with the Windows `start` command and exits the terminal immediately. It does not write logs, so it can be used from read-only media.
- `__UI_data__\run_ui.vbs` starts Electron through Windows Script Host without opening a terminal window. It does not write logs, so it can be used from read-only media.
- `__UI_data__\run_ui_debug.bat` starts Electron in the foreground, writes diagnostics to `__UI_data__\run_ui.log`, and keeps the terminal open so launch failures are visible.

Equivalent launcher copies are also generated inside `__UI_data__\app\` for backwards compatibility.

The generated root launchers use the bundled Electron executable:

```text
__UI_data__\app\node_modules\electron\dist\electron.exe
```

and pass the Electron app directory:

```text
__UI_data__\app\
```

## 13. User Guide

### 13.1 Generate or Update the UI on the Generator PC

Use the Windows PC that has:

- the repository checkout
- access to both video archives
- Python 3.12
- Node.js and npm
- ffmpeg
- `OPENAI_API_KEY` set when real GPT summaries need to be generated

Open PowerShell or Command Prompt:

```bat
cd C:\projects\video-library-smart-search
python -X utf8 ui_generator.py ui_generator.conf
```

The generator writes the UI bundle to:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\
```

Repeated runs are expected and safe:

- app files are refreshed
- existing valid thumbnails are reused
- existing real GPT 5.5 summaries are reused if input data did not change
- existing embeddings are reused if embedding source data did not change
- metadata JSON files are rewritten from the current source data

For the current production state, a repeat run should report:

```text
Thumbnail results: generated=0, reused=3310, missing_sources=0, failed=0
Prepared 414 day records; summaries generated=0, reused=414
Embedding cache is current
```

### 13.2 Generate Mock Summaries for Development

For development or smoke tests, set:

```ini
[summaries]
mock_gpt_api = true
```

Then run:

```bat
python -X utf8 ui_generator.py ui_generator.conf
```

Mock summaries are deterministic and do not call the OpenAI API.

Do not leave mock summaries in final production generation. Set:

```ini
mock_gpt_api = false
```

and rerun to generate real GPT summaries. The cache is mode-aware, so real runs do not reuse mock summaries.

### 13.3 Run the Electron App on the Generator PC

After generation, run:

```bat
D:\VideoLibrary\PrimaryArchive\__UI_data__\run_ui.bat
```

This starts the bundled Electron runtime. The batch launcher exits immediately after starting Electron, so a terminal window should not remain open while the UI is running. It does not require the repository checkout to be open and does not call Python.

For the cleanest double-click launch with no terminal window at all, run:

```bat
D:\VideoLibrary\PrimaryArchive\__UI_data__\run_ui.vbs
```

If Electron dependencies are missing, `run_ui.bat` prints the error in a terminal and waits for a key press. `run_ui.vbs` shows the same problem as a Windows message box.

If the app does not start, run the debug launcher:

```bat
D:\VideoLibrary\PrimaryArchive\__UI_data__\run_ui_debug.bat
```

Then inspect:

```text
D:\VideoLibrary\PrimaryArchive\__UI_data__\run_ui.log
```

The debug launcher is especially useful for mapped drives or remote PCs where Windows may block or crash the Electron GPU helper process before the UI appears.
Only `run_ui_debug.bat` creates `run_ui.log`; the normal `run_ui.bat` and `run_ui.vbs` launchers do not write log files and can be used from a read-only drive.

Use the app to:

- browse days
- filter SD/HD/both
- open day details
- search semantically and lexically
- see search results with compact date and time, for example `31.01.2008 14:23`
- open videos in the default Windows app
- open folders in Windows Explorer

### 13.4 Run the Electron App from a Remote PC with the Archive Disk Attached

Attach or map the disk containing both archives so their relative layout is preserved.

Example local disk or USB layout on the remote PC:

```text
X:\DV Capture (stará videokamera)\__UI_data__\
X:\Video.HD\
```

Example mapped network drive layout:

```text
X:\DV Capture (stará videokamera)\__UI_data__\
X:\Video.HD\
```

Run:

```bat
X:\DV Capture (stará videokamera)\__UI_data__\run_ui.bat
```

or, for a no-terminal launch:

```bat
X:\DV Capture (stará videokamera)\__UI_data__\run_ui.vbs
```

If the app does not start from the remote PC, run:

```bat
X:\DV Capture (stará videokamera)\__UI_data__\run_ui_debug.bat
```

Then inspect:

```text
X:\DV Capture (stará videokamera)\__UI_data__\run_ui.log
```

Only `run_ui_debug.bat` creates `run_ui.log`; use `run_ui.bat` or `run_ui.vbs` for normal read-only media launches.

The remote PC does not need the generator repository, Python, Node.js, npm, ffmpeg, or an OpenAI API key.

The first smart search can take longer on a remote PC because the bundled local embedding model is loaded from the attached or mapped drive. During this first load, the UI shows a status message so the user knows the app is working.

If videos do not open, confirm:

- both archive folders are present
- `Video.HD` is next to `DV Capture (stará videokamera)` at the same relative level
- Windows has a default app associated with `.avi`, `.mov`, `.m2ts`, and `.mts`

If search fails offline, confirm the model files are present:

```text
X:\DV Capture (stará videokamera)\__UI_data__\app\transformers_cache\intfloat\multilingual-e5-small\
```

### 13.5 Rebuild After New Video Descriptions

After `video_descr_generator.py` updates either production JSON:

```text
sample_data\primary_video_descriptions.json
sample_data\secondary_video_descriptions.json
```

rerun:

```bat
python -X utf8 ui_generator.py ui_generator.conf
```

Expected behavior:

- unchanged thumbnails are reused
- unchanged day summaries are reused
- changed or new day summaries are generated
- unchanged embedding index is reused when source text hash is unchanged
- changed or new embedding index is regenerated when source text changed

## 14. Open Questions

All major architecture decisions are currently closed. New open questions should be added only when implementation uncovers a concrete unresolved decision.

