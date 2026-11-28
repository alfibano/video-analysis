---
name: video-analysis
description: Analyse a local video file or one online video URL - what is SEEN (sampled frames, optional contact sheets, slide changes, exact-time frames) and what is SAID (offline timestamped transcript) - then describe both cross-referenced by timestamp. Use when the user asks to watch, read, analyse, summarise or extract rules from a video / mp4 / mkv / screen recording / course / webinar ("analyse cette vidéo", "lis la vidéo", "regarde cette vidéo", "que dit cette vidéo", "transcris la vidéo", "décortique la vidéo"). Local extraction works offline on Windows without system ffmpeg; transcript correction can use a configured AI service.
---

# Video analysis — what is seen, what is said

You have **no direct video or audio input**. You read **images extracted from the video** and a
**transcript of its audio**. Everything below turns a video into those two things, then tells you
how to read them without overclaiming.

## Confidentiality

**Privacy warning: transcript correction sends what is said in the video to an AI service outside
your PC, unless you declare a local AI. Do not use it on confidential recordings without consent.**

Transcript correction sends transcript text plus the video's title, description, language, and chapter
titles to the selected remote AI service, a third party outside the PC. Correction does not send video,
audio, frames, OCR text, or the API key. Auto prefers a detected Claude Code host, then a detected
Codex host, then OpenAI when an API key is present. Host providers use the signed-in account and need
no OpenAI key. For local media,
ask the user before running correction with remote AI and pass `--allow-remote-ai` only after a yes.
If the command exits 4, ask and rerun only after consent. Fetched public URLs are not asked. An
explicitly declared local AI (`--ai-locality local` or `VIDEO_ANALYSIS_AI_LOCALITY=local`) with a
base URL is not asked and needs no API key. Declaring local is a trust statement: a localhost proxy
may still forward data online. Use `--no-correct` to keep correction fully offline.

Writing a study sheet can send the bundle's text, metadata, OCR, and selected frame images to a
remote host model. It does not send the raw video or audio. Local media requires consent during
`prepare_sheet.py` before the host reads or writes sheet content; a fetched URL does not.

## Transcript correction providers

Select `--provider auto|claude-code|codex|openclaw|openai|local` (or
`VIDEO_ANALYSIS_CORRECT_PROVIDER`). `--host claude-code|codex|openclaw` or
`VIDEO_ANALYSIS_HOST` identifies the host whose configured model should guide correction. Auto
prefers a detected Claude Code host, then Codex, then OpenAI when an API key exists. OpenClaw has no
automatic host marker, so pass `--host openclaw` or set `VIDEO_ANALYSIS_HOST=openclaw`. Agents must
pass their known host explicitly to `analyze_video.py`. Without an available provider, correction
records a skip.

For a host provider, the resolver reads that host's configured session default and chooses a known
low-cost sibling from the same model family. Claude Code uses its resolved Claude model, Codex uses
the model in its config/profile, and OpenClaw uses `agents.defaults.model.primary` from its config.
Each host uses a known low-cost sibling when available; an unknown family uses that host's default.
OpenAI SDK correction uses its configured model, and local SDK correction retains its SDK default.
`--model` or `VIDEO_ANALYSIS_CORRECT_MODEL` explicitly selects a correction model. Before
correction starts, stderr shows `AI selection:` with host, model (or `host default`), role
(`low-cost` or `default`), reason, and locality. Tell the user those values before correction.

Only the first low-cost chunk can trigger one-time reachability fallback to the host session default.
Stderr then shows one `AI fallback:` notice, and that chunk plus the remaining chunks use the default.
Refusal, malformed output, validation failure, and other provider errors do not trigger this
fallback. Local media requires consent whenever correction's selected provider is remote, unless
the user has already granted consent or the command includes `--allow-remote-ai` after consent.
EOF or noninteractive output denies consent with exit 4.

Host locality applies only when the selected provider is that host provider. An OpenAI or local SDK
provider does not inherit locality from a detected host; correction locality comes from positive
provider evidence or `--ai-locality` / `VIDEO_ANALYSIS_AI_LOCALITY`. `VIDEO_ANALYSIS_HOST_AI` remains
the backward-compatible step-4 locality declaration and does not make correction local. Loopback
URLs alone are remote. A local SDK endpoint requires a base URL and an explicit local trust
declaration. Locality is a trust statement: a localhost proxy may still forward data online.

OpenClaw correction uses `openclaw infer model run --prompt-stdin [--model <provider/model>] --json`.
It sends the prompt on stdin and runs a one-shot provider completion without an agent turn or tools.
OpenClaw's live JSON envelope fields are unverified; offline coverage uses a fake runner. Correction
artifacts use schema 2 and include provider/model provenance for each segment. If chunks are refused,
report their count and ordinals and offer the printed `--retry-refused` command; never run escalation
automatically. A correction manifest from v0.3.0 lacks the new selection identity and exits 3 rather
than resuming. Rerun correction with the current documented route; use `--allow-remote-ai` only after
consenting if the local-media command requires it.

The step-4 writer always uses the session default model, never the correction low-cost sibling.
Before step-4 writing, tell the user the host, session default model, role `default`, reason, and
locality. Step-4 locality uses the selected host provider's resolved locality or the explicit
`--ai-locality` / `VIDEO_ANALYSIS_AI_LOCALITY` declaration; `VIDEO_ANALYSIS_HOST_AI` remains its
backward-compatible declaration for this step. Codex may require network-enabled execution to
reach its host service.

## Where things go — the owner's hard rule

All extracted material and study-sheet files go into **one working folder next to the video**:
`<folder of the video>\<video name>.analysis\` — never into a scratchpad, a temp dir or the project.
The owner deletes it when the work on that video is done, to reclaim disk space.
**Never delete it yourself.** If asked to clean up, use the `trash` skill on exactly that folder
(Recycle Bin, never `rm`). The study-sheet brief, JSON, and DOCX stay inside this folder.

Temporary and self-test work goes in the installed skill's `video-analysis/_temp` folder. The whole
`_temp` folder can be sent to the Recycle Bin when no run is in progress; scripts never delete it.
Set `VIDEO_ANALYSIS_TEMP_DIR` to an absolute path to redirect temporary work, e.g. for sandboxed or CI runs.
Analysis outputs remain in `<video>.analysis/` next to the video.

## Primary entry points

- `setup_tools.py` installs missing tools, checks readiness, and ends with the offline self-test;
  it prints `READY` only when setup and the self-test both succeed.
- `setup_tools.py --check` runs only the offline self-test: no install, readiness report, or model
  cache check.
- `analyze_video.py <url-or-path>` fetches a URL if needed, then extracts and builds the bundle.
  It prints `STEP fetch`, `STEP extract`, `STEP correct`, or `STEP bundle` on stderr, stops at the first failed
  step with that step's exit code, and accepts `--sub-langs` only with a URL.

Fetch progress and `OCR n/N` progress are bounded and printed on stderr.

## 1. Tools (once per machine)

```bash
python ~/.claude/skills/video-analysis/scripts/setup_tools.py --check   # offline self-test only
python ~/.claude/skills/video-analysis/scripts/setup_tools.py           # install what is missing
```
Installs only what is missing, with `pip --user` (no admin, no winget/choco): `Pillow`, `numpy`,
`faster-whisper`, `rapidocr`, `yt-dlp[default]`, `openai`, `python-docx`, and `imageio-ffmpeg` (a bundled ffmpeg binary) only when no
system ffmpeg is found; then caches the Whisper `small` model and warms OCR once so its model downloads
during setup. Tool resolution: environment override
(`VIDEO_ANALYSIS_FFMPEG`, `VIDEO_ANALYSIS_YTDLP`) → system PATH → bundled. YouTube also needs a
JavaScript runtime (Deno, else Node). Normal setup prints `READY` only after setup and the offline
self-test both succeed. `--check` runs only the offline self-test: it does not install tools, report
readiness, or check the model cache.
On machines with a detected NVIDIA GPU, setup also installs optional `nvidia-cublas-cu12` and
`nvidia-cudnn-cu12>=9,<10` (about 1 GB) to speed up Whisper. It reports `GPU speech: ready (CUDA)`
or `GPU speech: CPU only (<reason>)`; this readiness is informational. `setup_tools.py --check`
remains offline and does not install packages. Setup may download tools and models; fetch downloads
the requested public video; correction may send transcript text and context to the configured AI
service as described in Confidentiality.

Whisper model: Whisper is OpenAI's open speech-recognition AI, run locally via `faster-whisper`; the
*model* is its trained-knowledge file, downloaded once into the Hugging Face cache — afterwards the
audio never leaves the PC. Sizes `tiny`/`base`/`small`/`medium`: bigger = more accurate but slower;
default `small` (best CPU trade-off), `medium` ≈3x slower (`--model medium`).

Self-test (synthetic video and offline contract checks, `--full` adds Whisper):
`python ~/.claude/skills/video-analysis/scripts/selftest.py`

Optional real URL check (downloads and extracts a video):
`python ~/.claude/skills/video-analysis/scripts/selftest.py --network "<URL>"`

## Secondary tools

Study sheets use a separate host-writing turn after `analyze_video.py` has finished its bundle:

```bash
python ~/.claude/skills/video-analysis/scripts/prepare_sheet.py "<video path>"
python ~/.claude/skills/video-analysis/scripts/render_sheet.py "<video path>"
python ~/.claude/skills/video-analysis/scripts/render_sheet.py "<video path>" --template "<template.docx>"
```

Pass the user's requested output language explicitly with `prepare_sheet.py --language <user language>`;
use `auto` only when the user asks for that behavior. The preparation command prints the selected host
session default model. If it exits 4, ask for consent and rerun with `--allow-remote-ai` only after
approval. Read `sheet.brief.md`, `bundle.json`, comments when present, and the selected chapter frame
images. The current session writes `sheet.json` itself; sheet writing is never delegated.

Use schema v2 labels and content in the selected language. Give every chapter a short rationale for
its boundaries. Place several frames in a chapter when each adds distinct information, with each frame
beside its explanatory block. Keep corrections to lies or flagrant imprecision; ask a question only
when essential information is unavoidable. Complements may clarify without changing the author's
message. Web search is allowed to fact-check callouts and establish a series, but cite incontestable
sources such as official institutions, statistical offices, reference encyclopedias, or peer-reviewed
work. Never debunk religion, spirituality, or esotericism. Keep total callout text within the total
block-text budget. Summarize comments without names or identifiers; include an author email only when
the author published it in a description or slide. Never present inference as something said or shown.
The renderer displays script-owned facts for the writer's information; do not copy them into
`sheet.json`. Facts include deterministic metadata, safe description links, and a localized cost line
that distinguishes measured correction usage from estimated writing tokens at dated Anthropic list
prices; unsupported models show USD `n/a`.

Schema v1 sheets are refused with `regenerate sheet.json with the v0.6.0 brief`. Run
`render_sheet.py` and relay the resulting DOCX path. `analyze_video.py <URL> --comments N` collects up
to 100 top-level comments (default 100); `--comments 0` disables collection. Comments are optional,
are stored without commenter identity, and are summarized without names in the sheet.

Preparation/render exits: 2 means invalid or missing input; 3 means invalid sheet or a state/output
conflict; 4 means preparation consent is required (prepare) or successful preparation is missing
(render). For exit 3, explain the conflict; if an edited or untracked DOCX already exists, ask the
user to rename or move it before rerendering. A `.docx` or `.dotx` template is supported. Reruns
replace only their own unmodified DOCX; they never overwrite an edited DOCX.

`analyze_video.py` stops at `bundle.json`; preparation, the host writing turn, and DOCX rendering
are separate commands.

For an online video, fetch it first. Playlists and live/upcoming streams are refused before any
download. The video remains intact; SponsorBlock segments are marked in `sponsorblock.json` and
never removed.

```bash
python ~/.claude/skills/video-analysis/scripts/fetch_video.py "<URL>" --dest "<folder>"
```

By default subtitles for the exact video language, base language, base-orig variant, and English are requested in order with duplicates removed. Choose languages explicitly with --sub-langs fr en. Compatible reruns refresh missing video-language captions without downloading the video again. The video downloads first; subtitles download in a separate
best-effort pass, so subtitle errors (including rate limits) do not fail the fetch. Missing languages
are reported in a warning and recorded in the fetch manifest. Fetch writes `metadata.json`, sanitized
`raw_info.json`, available subtitle files, `sponsorblock.json`, and a v2 `manifest.json` into `<video>.analysis/`.
Downloads use short safe names: an ASCII lowercase title of up to 40 characters, cut at a word
boundary, followed by `_` and the video ID; the full stem is at most 60 characters. Local files
are never renamed.

For a local video, or after fetching a URL, extract frames and transcript:

```bash
python ~/.claude/skills/video-analysis/scripts/extract_video.py "<video path>" --language fr
python ~/.claude/skills/video-analysis/scripts/correct_transcript.py "<video path>" --language fr
python ~/.claude/skills/video-analysis/scripts/build_bundle.py "<video path>" --language fr
```
With `--transcribe auto` (the default), extraction uses the first usable video-language caption in
this order: manual exact language, base language, base-orig; then automatic captions in the same
order; then Whisper. Automatic captions remain eligible for correction; manual captions are left
unchanged. `--transcribe whisper` bypasses captions. Use `--device auto|cpu|cuda` to choose Whisper's
device when it runs.
For a local video using remote AI, ask before running the correction command above. If it exits 4,
ask the user and rerun with `--allow-remote-ai` only after a yes. `analyze_video.py` runs
`fetch → extract → correct → bundle`; its `--no-correct` option records an intentional skip. The
corrector accepts `--correct-model` through the pipeline (`--model` remains Whisper's model),
`--base-url`, and `--ai-locality local|remote`. It writes `transcript.corrected.json` without
changing original transcript or caption files. Changed correction settings or source are refused
with exit 3.
Run it in the **background** for videos longer than a few minutes. Whisper on CPU took about
1144 s for this 43-minute video; on an RTX 3050 Ti, CUDA int8_float16 took 273 s for the same
audio. A usable video-language caption avoids Whisper entirely. Options: `--transcribe auto|whisper`
(`whisper` forces speech recognition even when captions exist), `--device auto|cpu|cuda` (`auto` tries
CUDA then restarts the full transcript on CPU/int8 after a CUDA load or iteration failure), `--interval 5` (seconds between
frames; use 2 for fast-moving screens, 10-15 for talking heads), `--model small` (`medium` = better,
3x slower), `--skip-frames`, `--skip-transcript`, `--at HH:MM:SS[.mmm]` (repeatable, full-resolution
frame at an exact instant — used in step 3d).

Produces in `<video>.analysis\`: `frames\` (every interval) with their exact container timestamps
in `frames.json`, optional `sheets\` (3x3 contact sheets, each image stamped hh:mm:ss.mmm in yellow),
`keyframes.txt` + `stages.sheets.key_sheet_index` in `manifest.json` (key pages point to
`keysheets\` or reuse an identical-frame-list page in `sheets\`),
`audio.wav` when Whisper is used, `transcript.txt` (`[mm:ss] text`) + `transcript.json`, `at\`, `manifest.json`. Contact sheets are created only with `--contact-sheets`; `stages.sheets` records `sheets`, `keysheets`, and `key_sheet_index`. Add them later by rerunning extraction with `--contact-sheets` and the same other options; completed frames are reused. Transcript provenance is in `stages.transcript`: source, requested device, actual device, compute type, fallback reason, and requested transcribe mode. `auto` tries CUDA with `int8_float16`; after a CUDA model-load or segment-iteration failure it discards partial rows and restarts on CPU with `int8`. Old extraction manifests resume with current defaults; completed transcripts are reused whatever device flags are supplied. `--transcribe`, `--device`, and `--contact-sheets` do not change extraction identity. Re-running resumes and skips finished stages; changed extraction parameters or a
different video are **refused (exit 3)** instead of mixing outputs. Exit 1 = processing failure,
2 = bad input.

## 2b. Build the bundle

After extraction, run `build_bundle.py "<video path>"` (add `--language fr` to select captions,
or `--skip-ocr` when OCR is unavailable). It reads only files in `.analysis` and writes `bundle.json`,
which groups chapter speech, sampled frames with exact PTS, SponsorBlock overlap flags, and OCR text
read from key frames. Bundle schema 2 stores paths relative to the `.analysis` folder. A schema-1
`bundle.json` is refused with exit 3; send that file to the Recycle Bin and rerun `build_bundle.py`.
Transient key frames are not OCR'd and are marked `ocr_skipped: "transient"`. Sponsor speech and
frames remain represented and carry `is_sponsor`; sponsored speech is excluded from `speech_text`.
OCR is a text reading aid, not a visual description, and scans key frames only. Sampled frames can miss brief events, and an
empty chapter may have no frame; the bundle does not choose a nearest frame.

## 3. Read — in this order

If `bundle.json` exists, read it first instead of reopening raw files. It has chapter speech, frame
paths and PTS, sponsor flags, and key-frame OCR text. `ocr_text: null` means OCR was not run on that
frame; an empty string means it was scanned without finding text.

a. **The speech**: each chapter's `speech_text` in `bundle.json`, else the whole transcript
   (`transcript.txt`). In courses and demos, the decisive content (numbers, rules, exceptions,
   the real method) is very often **in the voice, not on screen**.
b. **The key-frame pages**, when extracted with `--contact-sheets`, following each `path` in `manifest.json` →
   `stages.sheets.key_sheet_index`. A page can be in `keysheets\` or reuse a matching contact page
   in `sheets\` (`reused_contact_sheet: true`). Older manifests without this index use `keysheets\`.
c. **The contact sheets** (`sheets\`), when extracted with `--contact-sheets`. They help follow
   motion on short videos. Add them to an existing extraction by rerunning with `--contact-sheets`
   and the same other options. For long videos, use `bundle.json` and chosen images in `frames\`
   (or `keyframes.txt`) as the reading index.
d. **Zoom where the speech points**: when the speaker says "here", "this candle", "look at…",
   re-run with `--at <timestamp>` **and the same options as the first run** (otherwise the manifest
   check refuses with exit 3), then read that full-resolution frame. Sheets are 640 px per cell —
   too small for chart labels or small text.
e. Write the analysis (step 4).

### Step-4 host locality contract

The study-sheet agent procedure above owns the step-4 writing turn. Use the selected host session
default model, and resolve locality from the selected host provider's positive locality evidence or
an explicit `--ai-locality` / `VIDEO_ANALYSIS_AI_LOCALITY` declaration. `VIDEO_ANALYSIS_HOST_AI=local|remote`
remains a backward-compatible step-4 declaration and does not make correction local. A loopback URL
alone is remote.

## 4. Report — keep three things apart

For each passage, cite **timestamps** and label every statement as one of:
- **ON SCREEN** — what an image actually shows (slide text, chart, annotation, UI).
- **SAID** — what the voice states (quote or close paraphrase, with `[mm:ss]`).
- **INFERENCE** — your interpretation linking the two. Never present it as seen or said.

Always include:
- the **gaps between what is written and what is said** (e.g. a slide says one thing and the voice
  adds the rule that actually matters, or contradicts it);
- what is **only shown once** (a single demonstrated example is an illustration, not evidence);
- **limits**: frame names `tSSSSSS.mmm.jpg` (e.g. `t000340.000.jpg`) come from container PTS;
  `frames.json` is authoritative. Sampling is adaptive by default (base grid plus frames around
  changes detected at 1 fps); `--fixed-interval` opts out. Changing the mode is refused by the
  manifest (exit 3). Sampling can still miss brief events; key-frame dedup can hide small
  edits; Whisper misspells names and jargon (check against the images); no reliable reading of tone,
  emotion, music, speaker identity or motion.

## 5. Afterwards

Tell the user the working folder path and its size (printed at the end of the extraction). Leave it
in place until they say the work on the video is finished; then trash it with the `trash` skill.

## Known pitfalls (already handled by the scripts)

- `faster_whisper` `model.transcribe("<path>")` raises
  `TypeError: open() got an unexpected keyword argument 'metadata_errors'` with recent PyAV. The script
  extracts 16 kHz mono PCM16 WAV with ffmpeg and passes a float32 array instead.
- `imageio-ffmpeg` ships ffmpeg but no ffprobe: duration and audio presence are read from ffmpeg's
  own banner.
- Scene detection via `select='gt(scene,N)'` + `showinfo` prints nothing under `-loglevel error`;
  key frames use thumbnail differences instead.
