# Platform evolution — design and delivery plan

Status: **steps 1 and 2 implemented; scene-aware/adaptive sampling and relative bundle paths delivered** (2026-11-24).

## Step 2 — aligned bundle

`build_bundle.py <video>` writes `bundle.json` inside the existing `.analysis` folder, using only
its metadata, captions, transcript, frame timestamps, key-frame list, and SponsorBlock intervals.
The video is used only for path/size/mtime identity. `params.bundle` and `stages.bundle` record the
settings and result; same-settings reruns resume, while mismatches or untracked bundle files exit 3.

Decisions: metadata chapters then deterministic fallback; manual VTT/SRT then automatic VTT/SRT,
then Whisper, then empty speech; OCR with RapidOCR on key frames only; sponsor content stays present
and is flagged, with sponsored speech omitted from `speech_text`. Output is `bundle.json` only.
There is no description timestamp parser, no bundle.md, and no nearest-frame fallback. OCR setup
warms the model once, while `--skip-ocr` records an intentional omission.

## Vision
Turn the `video-analysis` skill into a learning platform: from a local or online video, produce a
study sheet (PDF or PowerPoint) with introduction, summary, chapter-by-chapter text, the most
telling screenshot per chapter and a link to its time code. AI stays at the centre, but every
mechanical task is done by scripts (zero tokens); cheap or local models take the "simple but
smart" work; the chief model only writes the final synthesis from a prepared bundle.

| Stage | Who |
|---|---|
| Download, metadata, chapters, published subtitles, SponsorBlock skip-list | script (yt-dlp) |
| Frames with exact container timestamps, denser when the screen moves | script (ffmpeg) |
| On-screen text (OCR) | script |
| Transcript pass 1 | faster-whisper, local |
| Transcript pass 2 (low-confidence spans, glossary from metadata/OCR) | small local model (Gemma/Qwen), else a cheap API model |
| Pick the most telling frame per chapter | cheap model |
| Outline, themes, key moments, sheet text | chief model, text bundle + 6–20 frames |
| PDF / PPTX rendering, time-code links | script, template |
| Archive working folder as zip; recycle bin only on explicit option | script |

Owner decisions: video kept intact (sponsor segments listed, not cut); recycle bin only on an
explicit option; mobile app postponed (later a thin client); small local model on the PC for pass 2;
installable in minutes on Windows, macOS and Linux, reusing tools already present.

Step order: 1 fetch + metadata + exact timestamps → 2 per-chapter aligned bundle (speech + frames +
OCR) → 3 local transcript correction → 4 PDF/PPTX sheet → later: knowledge base, review, mobile.

## Step 1 — acceptance criteria
1. `fetch_video.py <url>` downloads one video (playlist and live refused; exit codes 0/1/2/3) and
   writes in `<video>.analysis/`: normalized `metadata.json` (title, author, channel, ISO upload
   date, duration, resolution/format, description, chapters, tags, url, extractor, id),
   `raw_info.json`, subtitles, `sponsorblock.json` (possibly empty). Video file intact.
2. `extract_video.py` then runs on the downloaded file without being refused by the folder guard.
3. Frame times come from container PTS (`frames.json`), used by sheets and `keyframes.txt`; on an
   offset-start video stamps match real time ±0.05 s.
4. Tool resolution: env override → system PATH → bundled `imageio-ffmpeg`; `yt-dlp` installed only
   if missing; JS runtime detected (deno, else node) for YouTube. Normal setup reports readiness;
   `setup_tools.py --check` runs only the offline self-test.
5. Self-test stays green offline and gains the checks below; optional `--network URL` real fetch.
6. SKILL.md, README, RELEASE_NOTES updated.

## Step 1 — decisions
- Shared `scripts/tools.py`: one resolution policy for fetch, extract, setup and self-test.
- Pure `scripts/metadata.py` (`normalize_metadata(info) -> dict`), tested offline on a fixture.
- Manifest v2: `{schema_version: 2, source, params: {fetch, extract}, stages}`; v0.1 flat manifests
  migrated transparently.
- Download destination: current directory, `--dest DIR` override; `restrictfilenames`, bounded
  title plus `_<id>`.
- Exit codes: 0 success, 1 tool/network/processing failure, 2 argument/input error, 3 playlist,
  live or manifest refusal.
- No `pyproject.toml` packaging in this step.

## Step 1 — tasks (test first for each)
1. **Tool resolution and conditional setup** — `tools.py`: `Tool(path, origin, version)`,
   `resolve_command`, `resolve_ffmpeg`, `resolve_js_runtime`, `resolve_ytdlp`. `setup_tools.py`
   adds `yt-dlp[default]`, installs `imageio-ffmpeg` only without system ffmpeg; `--check` prints
   origins; missing JS runtime fails readiness with guidance.
   Checks: *ffmpeg resolution precedence*, *JS runtime prefers deno then node*, *setup installs
   only missing dependencies*.
2. **Metadata normalization** — `metadata.py`, ISO date, absent values → null / empty lists.
   Check: *metadata normalization fixture*.
3. **Fetch and hand-off** — `fetch_video.py`: `build_ydl_options`, `refuse_unsupported`,
   `download_video`, `write_context`; preflight `download=False`; SponsorBlock marking only;
   source identity from the real post-download path. `load_manifest` compares `params.extract`
   only and preserves fetch data.
   Checks: *fetch manifest accepted by extract*, *v0.1 manifest remains compatible*, *playlist and
   live are refused before download*.
4. **Exact PTS** — `run_ffmpeg(..., loglevel)`, `parse_showinfo`, `extract_frames` with
   `-copyts` and `showinfo` at `-loglevel info` returning `{file: pts_s}` → `frames.json`;
   `frame_time` reads it, falls back to index × interval for old folders; labels with
   milliseconds. Check: *offset container PTS preserved* (`-output_ts_offset 2`).
5. **Self-test modes and docs** — `--network URL`; checks *default selftest is offline*,
   *documentation contract current*; update SKILL.md, README, RELEASE_NOTES.

Facts to verify at implementation: Python option name for JS runtimes (`js_runtimes`), exact
yt-dlp info-dict keys, `showinfo` behaviour on variable frame rate.
