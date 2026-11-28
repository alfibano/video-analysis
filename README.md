[English](README.md) · [Français](README.fr.md) · [Español](README.es.md)

# Video Digest

**Forty minutes of video, five minutes of reading.**<br>The essentials, structured and annotated — none of the detours.

- A study sheet organized into chapters, with boundaries explained.
- Video frames placed beside the text they illustrate.
- Faithful translation into the requested language.
- Correction, complement, and question callouts with numbered sources.
- A references list and a summary of viewer comments.
- For students and busy people who need the substance without watching everything.

## Features

Each sheet includes a concise summary sized to the video's length, chapters with a short rationale
for their boundaries, and several distinct frames when they add useful context. The DOCX places
each frame beside its related text in a two-column layout.

The writer preserves the speaker's meaning and translates faithfully into the requested language.
Callouts distinguish corrections, useful complements, and essential questions. Corrections and
complements cite numbered references; questions may need no source. The sheet ends with a
references list and summarizes comments without names or identifiers.

A facts header presents available video details such as duration, upload date, views, caption
languages, and a cost line. The cost line separates measured correction usage from estimated writing
tokens and uses dated Anthropic list prices; unsupported prices appear as USD n/a.

## Installation

Clone the repository and make its skill folder available to your host agent:

```powershell
git clone https://github.com/alfibano/video-digest.git
# Windows: create a user-level Claude Code skill link
mklink /J "%USERPROFILE%\.claude\skills\video-digest" "<clone>\video-digest"
```

For Codex or OpenClaw, install or link `video-digest/` as a skill using that host's skill setup.
The skill documents the workflow for Claude Code, Codex, and OpenClaw.

```powershell
python video-digest/scripts/setup_tools.py
python video-digest/scripts/setup_tools.py --check
```

Normal setup installs missing packages, supplies bundled ffmpeg if system ffmpeg is absent, caches
Whisper `small`, warms OCR, and runs the offline self-test. `--check` runs only the offline
self-test. YouTube downloads also need Deno or Node. Setup checks for ffmpeg, yt-dlp, and a JavaScript
runtime, and installs missing Python dependencies. Tested on Windows 11 with Python 3.12.

## Analyse a video

With the skill installed, ask Claude Code, Codex, or OpenClaw in plain words:

> Make a study sheet of https://www.youtube.com/watch?v=VIDEO_ID

The host agent runs the analysis pipeline, prepares and writes `sheet.json` in the current session,
then renders the editable Word document. The sheet files go in
`<video folder>/<video name>.analysis/`; the DOCX is
`<video name>.analysis/<video name>.docx`. For local videos, the skill asks before sending material
to a remote AI service.

The subsections below show the manual pipeline commands.

### URL

```powershell
python video-digest/scripts/analyze_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --dest "C:\Videos" --language en
```

The URL must identify one non-live video. Playlists and live or upcoming streams are refused.

### Local file

```powershell
python video-digest/scripts/analyze_video.py "C:\Videos\talk.mp4" --language fr
```

Local files are analyzed in place. Use `--language` to select transcript and caption language.

### Options

The command accepts these top-level options. Boolean switches are off unless noted.

| Option | Default and effect |
|---|---|
| `--dest` | Current directory; download folder for URLs only. |
| `--sub-langs` | Omitted: expand from the video's language; URL only. |
| `--comments` | `100`; top-level URL comments, range `0–100`; `0` disables. |
| `--interval`, `--dedup-threshold` | `5` seconds between base frames; `6` key-frame difference threshold. |
| `--model`, `--transcribe`, `--device` | Whisper `small`; `auto` uses captions first (`whisper` forces recognition); `auto` tries CUDA then restarts on CPU (also `cpu` or `cuda`). |
| `--correct-model`, `--host`, `--provider` | Provider's low-cost route or default; detect Claude Code/Codex host (OpenClaw explicit); provider `auto` (also `claude-code`, `codex`, `openclaw`, `openai`, `local`). |
| `--retry-refused`, `--base-url`, `--ai-locality` | Retry refused chunks (off); `OPENAI_BASE_URL` or unset; locality from provider evidence, otherwise remote. |
| `--allow-remote-ai`, `--no-correct` | Both off; allow remote correction of local media, or record an intentional correction skip. |
| `--language`, `--skip-frames`, `--contact-sheets`, `--skip-transcript` | Language `auto`; all switches off. Select transcript/captions language, omit frames or Whisper, or create contact/key-frame sheets. |
| `--at`, `--fixed-interval` | No timestamp; repeat for exact full-resolution frames. Fixed interval is off; adaptive sampling is default. |
| `--motion-threshold`, `--motion-margin` | `6` scene-change threshold; `1` second of context around detected changes. |
| `--ocr-min-score`, `--skip-ocr` | `0.5` minimum OCR confidence; skip switch off. |
| `--min-chapter`, `--max-chapter`, `--target-chapter` | `30`, `600`, and `300` seconds for minimum, maximum, and target fallback chapter lengths. |

## Output

All analysis and study-sheet files are saved beside the video in
`<video folder>/<video name>.analysis/`. A URL download is saved beside that folder, not inside it.
The scripts never delete outputs.

| File or folder | What it contains |
|---|---|
| `manifest.json` | Video identity, settings, and stage/resume state. |
| `metadata.json`, `raw_info.json`, `*.vtt` | Normalized metadata, sanitized source data, and available or requested subtitles. |
| `comments.json`, `sponsorblock.json` | Optional identity-free URL comments and sponsor marks; sponsored segments are never cut. |
| `frames/`, `frames.json`, `keyframes.txt` | Sampled frames with authoritative timestamps, and the list of key frames (screen changes). |
| `sheets/`, `keysheets/` | Optional 3×3 contact sheets and key-frame pages, only with `--contact-sheets`. |
| `audio.wav` | 16 kHz mono audio, only when Whisper runs. |
| `transcript.txt`, `transcript.json`, `transcript.corrected.json` | Timestamped speech from captions or Whisper; optional corrected speech includes provider/model provenance. |
| `at/` | Optional full-resolution frames from `--at`. |
| `bundle.json` | Chapter speech, timestamped frames, sponsor marks, and key-frame OCR. |
| `sheet.brief.md`, `sheet.json` | Writer instructions and the host-authored study sheet. |
| `<video stem>.docx` | Editable Word study sheet, optionally rendered with a template. |

## Processing steps

1. **Fetch** one URL video and its available metadata, subtitles, comments, and sponsor marks; local
   files skip this step.
2. **Extract** sampled frames and usable captions. Whisper runs only when captions are unavailable
   or `--transcribe whisper` is selected.
3. **Correct** selected transcript text when a correction provider is available; this step can be
   skipped.
4. **Build the bundle** with chapters, speech, frames, sponsor marks, and OCR text.
5. **Prepare the sheet** from the bundle and selected frame images.
6. **Write `sheet.json`** in a separate turn using the current host session.
7. **Render** the editable DOCX.

## Limits

| Setup | What to expect |
|---|---|
| Usable captions | Captions are preferred; Whisper does not run, so a GPU does not affect transcription. |
| No captions, NVIDIA GPU and CUDA wheels | Whisper uses CUDA `int8_float16`; if it fails, the partial output is discarded and transcription restarts on CPU `int8`. |
| No NVIDIA GPU | Whisper runs on CPU with `int8`. |

On the same 43-minute video, v0.6.5 reduced automatic analysis from 2,091 seconds to 518 seconds
using captions. Without captions, Whisper took 273 seconds on an RTX 3050 Ti with CUDA versus about
1,144 seconds on CPU. The analysis folder fell from 421 MB to 213 MB; optional CUDA libraries use
about 1 GB.

| AI configuration | What works |
|---|---|
| Claude Code or Codex host | The host can correct the transcript and write the study sheet using its configured session model. |
| OpenClaw host | Select it explicitly; it can correct the transcript and write the sheet. Live correction is marked unverified. |
| OpenAI or local SDK route | Transcript correction only; these SDK routes do not write the sheet. |
| No host or SDK provider | Extraction and `bundle.json` still work offline after setup. Correction is skipped and automatic sheet writing is unavailable. |

## Confidentiality

Remote transcript correction sends transcript text and limited video context, not raw video, audio,
frames, OCR text, or the API key. Remote sheet writing can receive bundle text, metadata, OCR, and
selected frames, but not raw video or audio. Local media requires consent before remote processing.
Use `--no-correct` to keep correction offline. Fetching a URL is a separate network action.

## Status

v0.6.5 measured `DEFAULT_CHECKS=145` (145 passed, 0 failed) and `FULL_CHECKS=147` (147 passed,
0 failed). See [release notes](RELEASE_NOTES.md).

## License

MIT — see [LICENSE](LICENSE).
