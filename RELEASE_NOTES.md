# Release notes — video-analysis

## Unreleased

### Added
- `fetch_video.py` downloads one video URL, refuses playlists/live streams, saves normalized and raw
  metadata, subtitles, SponsorBlock marks without removing video segments, and a v2 hand-off manifest.
- `extract_video.py` accepts fetch manifests, migrates v0.1 manifests, and writes container PTS to
  `frames.json` for timestamp labels.
- `setup_tools.py --check` reports tool paths and origins; self-test adds offline hand-off checks and
  optional `--network URL` fetch plus extraction.
- Temporary and self-test work uses the installed skill's `_temp` folder, with `VIDEO_ANALYSIS_TEMP_DIR`
  available to redirect it for sandboxed or CI runs.

### Fixed
- Subtitle downloads run after the video as a best-effort pass. Missing or rate-limited subtitles are
  warned about and recorded in the fetch manifest without failing a successful video download.

### Documentation
- `docs/selftest.md`: what the self-test checks, when to run it, usage, cleanup; presented in the
  README. The skill and README document URL fetch, tool setup, subtitles, and exact frame timestamps.

## v0.1.0 — 2026-11-14 — first release

A Claude Code skill that turns a local video into what an agent can read: sampled frames,
timestamped contact sheets, slide/screen changes, exact-time frames, and an offline timestamped
transcript, plus an operating procedure to describe **what is seen** and **what is said** without
overclaiming.

### Included
- `setup_tools.py` — installs `imageio-ffmpeg` (bundled ffmpeg, no system install, no admin rights),
  `Pillow`, `numpy`, `faster-whisper` with `pip --user`; caches the Whisper model; `--check`
  verifies without installing, model cache included.
- `extract_video.py` — one command, all outputs in a working folder **next to the video**
  (`<video>.analysis/`), so it can be deleted when the work is done:
  - frames every `--interval` seconds (default 5, decimals accepted);
  - 3×3 contact sheets, each frame stamped mm:ss;
  - key frames (screen changes) by grey-thumbnail difference, `--dedup-threshold` (default 6);
  - 16 kHz mono WAV and Whisper transcript (`.txt` + `.json`), audio start re-aligned on the
    video clock;
  - full-resolution frames at exact instants, `--at HH:MM:SS[.mmm]` (repeatable);
  - `manifest.json`: video identity, parameters, stage status; safe resume, refusal (exit 3)
    on a different video, changed parameters, or a non-empty folder without a manifest;
  - exit codes 0 / 1 / 2 / 3; nothing is ever deleted by the scripts.
- `selftest.py` — synthetic video, 10 checks including both refusal cases; `--full` adds Whisper.
- `SKILL.md` — procedure: read the whole transcript first, then key frames, then all sheets, then
  zoom at the instants the voice points to; report with ON SCREEN / SAID / INFERENCE labels and
  explicit limits.

### Workarounds built in
- `faster_whisper` `transcribe(path)` fails with recent PyAV
  (`open() got an unexpected keyword argument 'metadata_errors'`): audio is passed as an array.
- `imageio-ffmpeg` ships no ffprobe: duration and audio presence read from ffmpeg's banner.
- `select='gt(scene,N)'` + `showinfo` is silent under `-loglevel error`: key frames use thumbnails.

### Measured
- Self-test 10/10 on Windows 11, Python 3.12.
- Real run: 14 min 25 s screen-recorded trading course → 173 frames, 37 key frames,
  164 transcript segments, ~5 min on CPU, 43 MB working folder.

### Known limits
- Frame stamp = index × interval: approximate (± half an interval; more on variable frame rate).
- Key-frame dedup can hide small edits on a slide.
- The Whisper model is downloaded once into the standard Hugging Face cache (outside the working
  folder, by design).
- Network downloads are limited to one non-live video URL; playlists and live streams are refused.

---

## Roadmap — improvement axes (not started)

Gathered on 2026-11-14 from a survey of existing tools (unverified: found by web search, code not
read). Ordered by expected value.

1. **Alignment file screen ↔ speech** — one `segments.md`: for each key frame, its time, its image
   path and the transcript spoken until the next key frame. Biggest gain: the agent reads one
   document instead of cross-referencing sheets and transcript (fewer errors, fewer tokens).
   Seen in: slidegeist, slidoc, talkthrough-mcp.
2. **OCR of on-screen text** — titles, chart labels, figures become searchable text, often too
   small on contact sheets. Must install by pip only (no system Tesseract here): evaluate RapidOCR.
   Seen in: slidegeist (Tesseract), talkthrough-mcp, media-context-mcp, OcularAudio-MCP.
3. **Scene-aware sampling** — denser frames when the screen changes, sparser when static, with a
   frame cap; alongside the fixed interval. Seen in: claude-video (cap 100), claude-real-video.
4. **Detail levels** — transcript-only / key frames / full, to spend only what the question needs.
   Seen in: claude-video.
5. **SRT output** and word-level timestamps. Seen in: vidwise.
6. **MCP server form** — same pipeline exposed to any agent, not only Claude Code.
   Seen in: talkthrough-mcp, media-context-mcp, VideoContext MCP.
7. **Multilingual documentation** — README, docs and release notes in English (default), French
   and Spanish at least, with a language switcher at the top of each page. Owner request,
   2026-11-14.
8. **Local vision model** describing each frame (e.g. Qwen2.5-VL). Low priority: heavy on CPU and
   the agent already describes images. Seen in: Framesleuth.

Survey sources: [claude-video](https://github.com/bradautomates/claude-video) ·
[claude-real-video](https://www.everydev.ai/tools/claude-real-video) ·
[slidegeist](https://pypi.org/project/slidegeist/) · [slidoc](https://github.com/shyenx/slidoc) ·
[vidwise](https://pypi.org/project/vidwise/) ·
[talkthrough-mcp](https://glama.ai/mcp/servers/korovin-aa97/talkthrough-mcp) ·
[media-context-mcp](https://socket.dev/npm/package/media-context-mcp) ·
[Framesleuth](https://glama.ai/mcp/servers/santoshshinde2012/framesleuth) ·
[claude-watch](https://github.com/alexlarcheveque/claude-watch)
