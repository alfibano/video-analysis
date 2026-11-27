# Platform evolution — design and delivery plan

Status: **steps 1–4 implemented in v0.5.0; scene-aware/adaptive sampling, relative bundle paths, host-default sheet writing, and editable DOCX rendering delivered** (2026-11-27).

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
Turn the `video-analysis` skill into a learning platform: from a local or online video, produce an
editable Word `.docx` study sheet with introduction, summary, chapter-by-chapter text, the most
telling screenshot per chapter and a link to its time code. PDF/PPTX and other formats remain later
work. Scripts handle mechanical tasks; the selected host session default model writes the sheet from
a prepared bundle.

| Stage | Who |
|---|---|
| Download, metadata, chapters, published subtitles, SponsorBlock skip-list | script (yt-dlp) |
| Frames with exact container timestamps, denser when the screen moves | script (ffmpeg) |
| On-screen text (OCR) | script |
| Transcript pass 1 | faster-whisper, local |
| Transcript pass 2 (misheard words, glossary from metadata/OCR) | selected host's resolved session default or its known same-family low-cost sibling |
| Pick the most telling frame per chapter | host agent after viewing the chapter frames |
| Sheet outline, themes, key moments, and text in `sheet.json` | selected host session default model, text bundle + selected frames |
| Validate and render editable DOCX with time-code links | script, built-in formatting or `.docx` / `.dotx` template |
| Archive working folder as zip; recycle bin only on explicit option | script |

Owner decisions: video kept intact (sponsor segments listed, not cut); recycle bin only on an
explicit option; mobile app postponed (later a thin client); cheap online model for pass 2 (local
models dropped after the 2026-11-26 benchmark); installable in minutes on Windows, macOS and Linux,
reusing tools already present.

Step order: 1 fetch + metadata + exact timestamps → 2 per-chapter aligned bundle (speech + frames +
OCR) → 3 transcript correction (cheap online model) → 4 `prepare_sheet.py` → host writes
`sheet.json` → `render_sheet.py` produces editable DOCX. Later: PDF/PPTX, provider adapter/UI,
knowledge base, review, and mobile.

## Step 3 — correction model benchmark (2026-11-26)
Whisper `small` output corrected in chunks of 40 segments with one prompt, scored as word error
rate (WER) against human subtitles: an 8-min French science video and a 20-min English TED talk.
Local models ran with Ollama on a laptop GPU with 4 GB of memory and 16 GB of RAM.

| Corrector | WER fr (raw 9.0 %) | WER en (raw 8.0 %) | Time per video hour | Cost per video hour |
|---|---|---|---|---|
| GPT Luna, online | 6.7 % | 7.5 % | ~8 min, sequential calls | ~$0.015 |
| Claude Haiku 5.5, online | 6.8 % | 7.7 %, one chunk refused as copyrighted | ~8 min | ~$0.03–0.04 |
| Gemma 4 E4B, local | 7.5 % | 7.8 % | ~32–42 min | none |
| Qwen 3 4B instruct, local | 8.1 % | not measured | ~72 min | none |
| Whisper `medium`, no correction | 7.6 % | not measured | — | none |

Decision: pass 2 uses a cheap online model. The local models corrected less, ran about five times
slower, did not fit in GPU memory and saturated RAM; an online model is needed anyway for the
sheet (step 4). Part of the remaining WER is editorial: human subtitles drop fillers and lightly
reword, which a correction must not reproduce.

Correction resolves a configured session default for Claude Code, Codex, or explicitly selected
OpenClaw and uses a known same-family low-cost sibling when available. OpenClaw is supported but
live-unverified; its offline tests use a fake runner. OpenAI remains the SDK route outside a host,
with explicitly declared local endpoints as an option. A first-chunk reachability failure can switch
once from low-cost to the session default. Only refused chunks may be escalated to a family sibling
or the session default; the user must run the offered command. Step 4 always uses the session default
model, while correction may use its low-cost sibling. The study-sheet stage now uses the selected
host session default model and is delivered in v0.5.0.

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
