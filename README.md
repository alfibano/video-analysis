# video-analysis

A [Claude Code](https://claude.com/claude-code) skill that lets an agent **read a local video**:
what is **seen** (sampled frames, timestamped contact sheets, screen changes, exact-time frames) and
what is **said** (offline timestamped transcript), with a procedure to report both cross-referenced
by timestamp — keeping what is on screen, what is said, and what is inferred apart.

Local analysis works offline after a one-time model download. Transcript correction can send text
to a configured AI service; see [Confidentiality](#confidentiality) before enabling it. URL fetching
is an explicit network step. No system ffmpeg or admin rights: tested on Windows 11 with Python 3.12.

## Confidentiality

**Privacy warning: transcript correction sends what is said in the video to an AI service outside
your PC, unless you declare a local AI. Do not use it on confidential recordings without consent.**

With an API key, transcript correction sends transcript text plus the video's title, description,
language, and chapter titles to the configured AI service (OpenAI by default), a third party outside
your PC. Correction does not send video, audio, frames, OCR text, or the API key. For a local video,
the tool asks before sending; if nobody can answer, it stops with exit 4 and requires
`--allow-remote-ai` after consent. Fetched public URLs are not asked. An explicitly declared local
AI (`--ai-locality local` or `VIDEO_ANALYSIS_AI_LOCALITY=local`) with a base URL is not asked and
needs no API key. Declaring local is a trust statement: a localhost proxy may still forward data
online. Use `--no-correct` or omit the API key to keep correction fully offline.

**About the Whisper model.** The transcript is produced by Whisper, OpenAI's open speech-recognition
AI, run locally through `faster-whisper`. Its *model* is the trained-knowledge file the program needs:
`setup_tools.py` downloads it once into the Hugging Face cache, after which transcription is offline
and the audio never leaves your PC. Sizes `tiny`, `base`, `small`, `medium`: bigger is more accurate
but slower. `small` is the default (best trade-off on a CPU); `medium` is about 3x slower. The
self-test skips transcription unless you pass `--full`.

## Install

```bash
git clone https://github.com/alfibano/video-analysis.git
# make the skill visible to Claude Code (user level), e.g. on Windows:
#   mklink /J "%USERPROFILE%\.claude\skills\video-analysis" "<clone>\video-analysis"
# or copy the video-analysis/ folder into ~/.claude/skills/
python video-analysis/scripts/setup_tools.py        # installs tools and warms Whisper/OCR models
python video-analysis/scripts/setup_tools.py --check # runs only the offline self-test
python video-analysis/scripts/selftest.py           # offline checks on a synthetic video
```

## Self-test

`selftest.py` checks in one command, without a real video, that the skill works on your machine:
it builds a 20 s synthetic video, runs the extraction on it and reports `PASS`/`FAIL` on named checks,
including cases where the tool must **refuse**. Run it after installing and before every
release; `--full` also tests transcription. The default run stays offline; `--network URL` opts into
a real fetch and extraction (`python video-analysis/scripts/selftest.py --network "<url>"`). Details:
[docs/selftest.md](docs/selftest.md).

## Primary entry points

- `setup_tools.py` installs missing tools, checks readiness, and ends with the offline self-test;
  it prints `READY` only when setup and the self-test both succeed.
- `setup_tools.py --check` runs only the offline self-test: no install, readiness report, or model
  cache check.
- `analyze_video.py <url-or-path>` fetches a URL if needed, then extracts and builds the bundle.
  It prints `STEP fetch`, `STEP extract`, `STEP correct`, or `STEP bundle` on stderr, stops at the first failed
  step with that step's exit code, and accepts `--sub-langs` only with a URL.

Fetch progress and `OCR n/N` progress are bounded and printed on stderr.

## Analyse a video

In Claude Code, just ask: "analyse this video: <url or path>". The agent runs one command, which
you can also run yourself:

```bash
python video-analysis/scripts/analyze_video.py "https://www.youtube.com/watch?v=<id>" --dest "<folder>" --language en
python video-analysis/scripts/analyze_video.py "<folder>/talk.mp4" --language fr
```

With a URL, four steps run in order; with a local file, fetch is skipped. Each step
announces itself on stderr (`STEP fetch`, `STEP extract`, `STEP correct`, `STEP bundle`), and the command stops at
the first failed step with that step's exit code. Everything except the downloaded video lands in
one working folder next to the video, `<video folder>/<video name>.analysis/`, so it can be deleted
in one go when you are done; the scripts never delete anything. Measured on a 23-minute talk:
4 min 26 s from URL to `bundle.json`.

### `STEP fetch` — download (URL only)

One non-live video; playlists and live or upcoming streams are refused before any download. This step
downloads one public video. Progress is bounded.

| Output | Content |
|---|---|
| `<folder>/<title>_<id>.mp4` | The video, intact: sponsored segments are marked, never cut. `--dest` chooses the folder (default: current folder). Short safe name: ASCII lowercase title of up to 40 characters, cut at a word boundary, then `_` and the video ID; the stem is at most 60 characters. Local files are never renamed. |
| `metadata.json` | Normalized metadata: title, channel, upload date, duration, chapters, resolution, tags, description, URL. |
| `raw_info.json` | Full platform information, sanitized. |
| `<video>.<lang>.vtt` | Subtitles in the video's language and English when available; `--sub-langs fr en` chooses others. Downloaded in a separate best-effort pass: a missing or rate-limited language is a warning, not a failure. |
| `sponsorblock.json` | Sponsored segments reported by SponsorBlock. |
| `manifest.json` | Hand-off record (v2): video identity, fetch settings, subtitle languages saved and missing. |

### `STEP extract` — what is seen and what is said (offline)

| Output | Content |
|---|---|
| `frames/` + `frames.json` | Sampled frames named by timestamp, `tSSSSSS.mmm.jpg` (e.g. `t000340.000.jpg`); `frames.json` holds the exact container timestamps and is authoritative. Sampling is adaptive by default: a base grid every `--interval` seconds (default 5) plus frames around screen changes detected at 1 fps; `--fixed-interval` opts out. |
| `sheets/` | 3×3 contact sheets, each image stamped hh:mm:ss.mmm. |
| `keyframes.txt` + `keysheets/` | Only the frames that visibly changed: one per slide or screen. |
| `audio.wav` | 16 kHz mono audio track. |
| `transcript.txt` + `transcript.json` | Offline Whisper transcript, `[mm:ss] text` (`--model`, default `small`; `--skip-transcript` to omit it). |
| `at/` | Full-resolution frames at exact instants, `--at HH:MM:SS[.mmm]` (repeatable). |
| `manifest.json` | Updated with the extraction settings and stage status. |

### `STEP correct` — transcript correction (optional AI service)

When `OPENAI_API_KEY` is set, the selected automatic captions or Whisper transcript can be corrected
in bounded chunks. The original transcript and caption files are never changed. `--no-correct`
records a skip; without a key correction is skipped. See [Confidentiality](#confidentiality) for
what leaves the PC and local-video consent.

| Output | Content |
|---|---|
| `transcript.corrected.json` | Corrected text beside each original segment, with source, model, endpoint locality, and per-segment status. Manual subtitles retain precedence and are not corrected. |
| `manifest.json` | Correction parameters and stage status, consent method when needed, segment status counts, and token usage. |

### `STEP bundle` — the reading summary (offline)

| Output | Content |
|---|---|
| `bundle.json` | One entry per chapter: the speech with timestamps, the frames with their exact timestamps, sponsor flags, and the text read on key frames (OCR, optical character recognition). |
| `manifest.json` | Updated with the bundle settings and stage status. |

Chapters come from the video's own chapters, otherwise from an automatic split (`--min-chapter`,
`--max-chapter`, `--target-chapter`). Speech comes from manual subtitles in the chosen language,
then automatic captions, else the Whisper transcript. Sponsored speech stays in the bundle, flagged,
and is left out of each chapter's summary-ready `speech_text`. OCR reads key frames only; transient
key frames (quickly replaced by the next one) are skipped and marked `ocr_skipped: "transient"`;
`--skip-ocr` omits OCR. Bundles use schema 2, with paths relative to the `.analysis` folder; a
schema-1 `bundle.json` is refused with exit 3: send that file to the Recycle Bin and run again.
Read `bundle.json` first when present.

### Rerunning

Running the same command again resumes: finished stages are skipped. Changed settings or a
different video are refused (exit 3) instead of mixing outputs; exit 1 is a processing failure,
exit 2 bad input, and exit 4 means local-video consent for remote AI is required. Options include
`--correct-model` (separate from Whisper's `--model`), `--base-url`, `--ai-locality local|remote`,
`--allow-remote-ai` (use only after consent), and `--no-correct`. Correction also reads
`VIDEO_ANALYSIS_CORRECT_MODEL`, `OPENAI_BASE_URL`, and `VIDEO_ANALYSIS_AI_LOCALITY`. Each step also
exists as its own script — `fetch_video.py`, `extract_video.py`, `correct_transcript.py`,
`build_bundle.py` — to rerun one step alone, for example
`extract_video.py "<video>" --at 00:05:40` with the same options as the first run. All options:
[`video-analysis/SKILL.md`](video-analysis/SKILL.md).

Temporary and self-test work goes in the installed skill's `video-analysis/_temp` folder. The whole
`_temp` folder can be sent to the Recycle Bin when no run is in progress; scripts never delete it.
Set `VIDEO_ANALYSIS_TEMP_DIR` to an absolute path to redirect temporary work, e.g. for sandboxed or CI runs.
Analysis outputs stay in `<video>.analysis/` next to the video.

## Status

v0.3.0 — see [RELEASE_NOTES.md](RELEASE_NOTES.md), including the improvement roadmap.

## License

MIT — see [LICENSE](LICENSE).
