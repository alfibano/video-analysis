# Release notes — video-analysis

## v0.6.2 — 2026-11-27 — uniform text size

### Fixed
- The facts header table forced 8 pt text while its links stayed at body size. All non-heading text
  now inherits the body (or user template) size.

## v0.6.1 — 2026-11-27 — undistorted sheet images

### Fixed
- Study-sheet frames kept a 1-inch-wide height and were then widened, so every image was stretched
  (3.7:1 instead of 16:9). Images are now inserted at their final width and keep their aspect ratio;
  the DOCX layout check asserts the ratio.

## v0.6.0 — 2026-11-27 — bounded comments and localized study sheets

### Added
- URL fetches collect up to 100 top-level comments by default; `--comments 0` disables collection.
  Persisted comments contain no commenter identity. Sheet schema v2 adds deterministic facts, context,
  multiple chapter blocks/frames, callouts, references, and localized labels.
- DOCX facts are formatted for readers: durations, grouped counts, one-decimal MB values, and safe
  description links in a single row. The cost line separates measured correction tokens from estimated
  writing tokens, uses two-decimal USD and Anthropic list prices dated 2026-11-03, and shows `n/a` when
  USD cannot be supported.

### Changed
- Writers must pass the user's requested language through `prepare_sheet.py --language`. The brief
  makes renderer-owned facts informational and says not to copy them into `sheet.json`. Schema-v1
  sheets now require regeneration with the v0.6.0 brief.
- Editorial rules restrict corrections to lies or flagrant imprecision, reserve questions for essential
  unavoidable information, require incontestable sources, and prohibit debunking religion, spirituality,
  or esotericism. Comments are summarized without names; author email is allowed only when published
  by the author. Several distinct frames may appear in each chapter beside their related text.
- Live DOCX review found and fixed two integration issues: yt-dlp comment arguments are strings, and
  the writer brief is persisted as exact UTF-8 bytes.
- Fetch resume now matches extractor/video identity and rejects changed subtitle languages; sheet cost
  estimates use Anthropic list prices only for the native Anthropic endpoint.

### Measured
- Default self-test: `DEFAULT_CHECKS=132` (132 passed, 0 failed).
- `--full`: `FULL_CHECKS=134`; four Whisper-dependent checks failed in this sandbox because its model
  snapshot was unavailable. The remaining checks passed.

## v0.5.0 — 2026-11-27 — editable DOCX study sheets

### Added
- `prepare_sheet.py` resolves the host session default model, enforces local-media remote-AI consent,
  and writes a reusable `sheet.brief.md` with schema-v1 instructions. The host agent writes
  `sheet.json`; `render_sheet.py` validates it and creates an editable `.docx`.
- Schema-v1 sheet labels are required and supplied in the sheet language. Auto language resolves
  from `bundle.json` speech language when present, otherwise the brief tells the writer to use the
  speech language.
- Built-in rendering and user `.docx` / `.dotx` templates preserve editable Word content. Supported
  YouTube and Vimeo sources receive timestamp seek links.
- Source URLs must match `bundle.json`; only safe HTTP(S) URLs become clickable DOCX links.
- Manifest resume identity and no-overwrite behavior protect existing DOCX files. Added
  `python-docx` as a rendering dependency and 27 offline sheet checks.

### Changed
- Study-sheet writing may share bundle text, metadata, OCR, and selected frame images with a remote
  host; raw video and audio are not sent. Local media requires consent during preparation.
- `analyze_video.py` still stops at `bundle.json`; preparation, host writing, and DOCX rendering are
  separate stages. The host writer is the current session default model and is never delegated.

### Measured
- Default self-test: `DEFAULT_CHECKS=114` (114 passed, 0 failed).
- `--full`: `FULL_CHECKS=116` (116 passed, 0 failed).

### Deferred
- Standalone provider adapters, a separate UI, PDF/PPTX and other export formats, and blind
  writer-model benchmarks remain future work.

## v0.4.0 — 2026-11-27 — host-aware model routing

### Added
- Host-aware correction for Claude Code, Codex, and explicitly selected OpenClaw. The resolver
  reads each host's configured session default and selects a known low-cost sibling from the same
  family; unknown families use the host default. OpenAI and local SDK routes retain their own model
  behavior.
- One stderr selection notice before correction, with host, model (or `host default`), role, reason,
  and locality. Only a transport or model-unavailable failure on the first low-cost chunk triggers
  one switch to the session default; the fallback notice is printed once and the remaining chunks
  use that default. Refusals and invalid output do not trigger a switch.
- Selection provenance and strict resume identity for host, provider, model, endpoint identity, and
  locality. A v0.3.0 correction manifest lacks the identity and exits 3 rather than resuming.
- Refusal escalation follows the model family and remains user-triggered, limited to refused chunks.

### Changed
- Locality is local only with positive provider evidence or an explicit locality declaration; a
  loopback endpoint alone remains remote. Host locality applies to correction only when the selected
  provider is that host provider. `VIDEO_ANALYSIS_HOST_AI` remains the backward-compatible step-4
  declaration and does not make correction local.
- Step 4 uses the session default model; correction may use its low-cost sibling. The model, role,
  reason, and locality are disclosed before correction and before step-4 writing.
- OpenClaw uses `openclaw infer model run --prompt-stdin [--model <provider/model>] --json`. The
  prompt travels on stdin; this one-shot provider command starts no agent turn and loads no tools.
  OpenClaw correction is `UNVERIFIED-LIVE`; its live envelope fields are unverified and coverage
  uses an offline fake runner.

### Measured
- Self-test: default 88/88; `--full` 90/90.

## v0.3.0 — 2026-11-27 — transcript correction

### Added
- Host-account correction providers: Claude Code Haiku and Codex Luna, selected before OpenAI in
  `auto` (refused chunks can be retried with Sonnet or Sol); explicitly declared local SDK endpoints can omit the API key.
- Schema-v2 correction provenance, refused-chunk ordinals, attempt accounting, and selective
  `--retry-refused` escalation commands. Escalation is offered and never automatic.
- `correct_transcript.py` corrects selected automatic-caption or Whisper speech in bounded chunks,
  records segment statuses and token usage, and preserves every original transcript and caption file.
- `analyze_video.py` runs `fetch → extract → correct → bundle`; correction can use OpenAI or a
  compatible configured endpoint, including an explicitly declared local endpoint.
- Local media requires consent before correction sends transcript text and video context to remote
  AI. A refusal stops with exit 4 before a provider call; `--no-correct` skips correction.
- Bundles use only corrected speech whose source and segment content still match, and record its
  provenance. Bundle resume detects a changed selected speech input.

### Changed
- Pipeline and setup expose provider readiness without printing credentials. Remote host correction
  sends transcript text and title/description/language/chapter context only; schema-v1 corrections
  are ignored and original speech remains the fallback.
- `setup_tools.py` installs and reports the OpenAI SDK, host CLI readiness, and API-key presence
  without printing the key. A missing key skips OpenAI correction without failing setup; host
  providers use the authenticated host account.
- For local media, remote correction sends transcript text, title, description, language, and chapter
  titles to the configured service. It does not send video, audio, frames, OCR text, or the key.
- Migration: bundles built by v0.2.x are refused with exit 3 once correction changes the speech
  input. Send that bundle's `bundle.json` to the Recycle Bin and rerun.

### Documentation
- README and the skill document confidentiality, correction options, consent, locality declarations,
  corrected output, and the future step-4 host locality contract.
- Self-test documentation lists correction, provenance, readiness, and privacy contract checks; the
  default self-test makes zero provider calls.

### Measured
- Self-test: default 75/75; `--full` 77/77.
- Live, inside Claude Code (Haiku through the signed-in account), 8-min French video given as a
  local file: without consent the pipeline stops with exit 4 before any call; with
  `--allow-remote-ai` correction took about 20 s, changed 35 of 168 segments, refused none, and
  lowered word error rate against human subtitles from 9.0 % to 7.2 %.
- Live, local Qwen 3 4B through Ollama (`--provider local`): the SDK path works without a key;
  one chunk timed out and kept its original text, which led to the longer local timeout.

## v0.2.1 — 2026-11-26 — SponsorBlock fix
### Fixed
- `build_bundle.py` no longer crashes when `sponsorblock.json` lists segments: SponsorBlock spans are normalized before chapters are built. Before, any video with sponsor, intro or other SponsorBlock segments failed at `STEP bundle`.
### Documentation
- README describes the one-command workflow and the files each step writes.
- Platform plan: transcript correction (step 3) uses a cheap online model; benchmark recorded.
### Measured
- A 20-min TED talk with three SponsorBlock spans, which crashed in v0.2.0, now bundles into
  10 chapters with all three spans in `sponsor_overlap`; self-test 50/50, 52/52 with `--full`.

## v0.2.0 — 2026-11-26 — online videos and reading bundle

From one URL or local file, `analyze_video.py` downloads (when needed), extracts and writes a
chapter-by-chapter `bundle.json`: speech, frames with exact timestamps, sponsor marks and text read
on slides.

- Bundle schema v2 stores source, speech, and frame paths relative to the `.analysis` folder.
- Online video downloads use short ASCII filenames derived from the title and video ID.
- OCR uses a bounded ONNX thread count and reports throttled progress.
- Fetch reports bounded yt-dlp progress for terminal and redirected output.
- Adaptive sampling keeps the base grid and frames around detected changes; --fixed-interval opts out.
- Sampled frame names encode rounded PTS milliseconds; frames.json remains authoritative.
- Added analyze_video.py for local and URL fetch → extract → bundle workflows.
- setup_tools.py --check now runs only the offline self-test; normal setup runs it after readiness.
- Adaptive bundle OCR skips transient key frames followed within the sampling window.

### Added
- `build_bundle.py` creates a chapter-aligned `bundle.json` from `.analysis` files, with subtitle/Whisper speech, frame PTS, sponsor flags, and key-frame OCR. It resumes identical settings and refuses manifest conflicts.
- `setup_tools.py` installs RapidOCR when missing, reports OCR readiness, and warms its model during normal setup; bundling supports `--skip-ocr`.
- `fetch_video.py` downloads one video URL, refuses playlists/live streams, saves normalized and raw
  metadata, subtitles, SponsorBlock marks without removing video segments, and a v2 hand-off manifest.
- `extract_video.py` accepts fetch manifests, migrates v0.1 manifests, and writes container PTS to
  `frames.json` for timestamp labels.
- `extract_video.py` decodes one RGB frame pass for adaptive or fixed sampling and records sampling
  settings in the extraction manifest.
- `setup_tools.py --check` runs only the offline self-test; self-test adds offline hand-off checks and
  optional `--network URL` fetch plus extraction.
- Temporary and self-test work uses the installed skill's `_temp` folder, with `VIDEO_ANALYSIS_TEMP_DIR`
  available to redirect it for sandboxed or CI runs.

### Fixed
- Subtitle downloads run after the video as a best-effort pass. Missing or rate-limited subtitles are
  warned about and recorded in the fetch manifest without failing a successful video download.
- Frame names now use container PTS; `frames.json` is authoritative instead of estimating time from
  the sample index and interval.

### Documentation
- `docs/selftest.md`: what the self-test checks, when to run it, usage, cleanup; presented in the
  README. The skill and README document URL fetch, tool setup, subtitles, and exact frame timestamps.
- The skill and README document the fetch → extract → bundle workflow and bundle-first reading.

### Measured
- On a real 23-min talk, fixed extraction took 42 s and adaptive extraction took 73 s (ratio 1.74).
- OCR was about 28% faster with bounded ONNX threads.
- Parallel OCR workers were rejected after no gain was measured.
- Splitting extraction into parallel portions was rejected after gains of 30 s or less.
- Release run (same talk, URL to bundle, subtitles instead of Whisper): 266 s end to end;
  469 frames, 110 key frames of which 30 transient frames were not OCR'd; self-test 49/49, 51/51 with `--full`.

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

## Roadmap — improvement axes

Gathered on 2026-11-14 from a survey of existing tools (unverified: found by web search, code not
read). Ordered by expected value.

1. **Detail levels** — transcript-only / key frames / full, to spend only what the question needs.
   Seen in: claude-video.
2. **SRT output** and word-level timestamps. Seen in: vidwise.
3. **MCP server form** — same pipeline exposed to any agent, not only Claude Code.
   Seen in: talkthrough-mcp, media-context-mcp, VideoContext MCP.
4. **Multilingual documentation** — README, docs and release notes in English (default), French
   and Spanish at least, with a language switcher at the top of each page. Owner request,
   2026-11-14.
5. **Local vision model** describing each frame (e.g. Qwen2.5-VL). Low priority: heavy on CPU and
   the agent already describes images. Seen in: Framesleuth.

Survey sources: [claude-video](https://github.com/bradautomates/claude-video) ·
[claude-real-video](https://www.everydev.ai/tools/claude-real-video) ·
[slidegeist](https://pypi.org/project/slidegeist/) · [slidoc](https://github.com/shyenx/slidoc) ·
[vidwise](https://pypi.org/project/vidwise/) ·
[talkthrough-mcp](https://glama.ai/mcp/servers/korovin-aa97/talkthrough-mcp) ·
[media-context-mcp](https://socket.dev/npm/package/media-context-mcp) ·
[Framesleuth](https://glama.ai/mcp/servers/santoshshinde2012/framesleuth) ·
[claude-watch](https://github.com/alexlarcheveque/claude-watch)
