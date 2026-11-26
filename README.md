# video-analysis

A [Claude Code](https://claude.com/claude-code) skill that lets an agent **read a local video**:
what is **seen** (sampled frames, timestamped contact sheets, screen changes, exact-time frames) and
what is **said** (offline timestamped transcript), with a procedure to report both cross-referenced
by timestamp — keeping what is on screen, what is said, and what is inferred apart.

Local analysis works offline after a one-time model download. URL fetching is an explicit network
step. No system ffmpeg or admin rights: tested on Windows 11 with Python 3.12.

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
  It prints `STEP fetch`, `STEP extract`, or `STEP bundle` on stderr, stops at the first failed
  step with that step's exit code, and accepts `--sub-langs` only with a URL.

Fetch progress and `OCR n/N` progress are bounded and printed on stderr.

## Secondary tools

In Claude Code: "analyse this video: <path>". Or directly:

```bash
python video-analysis/scripts/extract_video.py "<video>" --language fr --interval 5
```

For a URL, download one non-live video first, then extract it:

```bash
python video-analysis/scripts/fetch_video.py "<url>" --dest "<folder>"
python video-analysis/scripts/extract_video.py "<folder>/<video>.mp4" --language fr --interval 5
python video-analysis/scripts/build_bundle.py "<folder>/<video>.mp4" --language fr
```

Fetch writes normalized `metadata.json`, sanitized `raw_info.json`, available subtitle files,
`sponsorblock.json` (segments marked, never removed), and a v2 manifest. Extract writes container
timestamps in `frames.json` alongside the frames and transcript. Frame names `tSSSSSS.mmm.jpg`
(e.g. `t000340.000.jpg`) come from container PTS; `frames.json` is authoritative. Sampling is
adaptive by default (base grid plus frames around changes detected at 1 fps); `--fixed-interval`
opts out, and changing the mode is refused by the manifest (exit 3). Bundle writes `bundle.json` with
chapter speech, frame PTS, SponsorBlock flags, and OCR text from key frames. Read `bundle.json` first
when present; use `--skip-ocr` to omit OCR. Sponsor intervals stay intact, and sponsored speech is
flagged and omitted from each chapter's summary-ready `speech_text`.

Everything goes to `<video folder>/<video name>.analysis/` — frames, sheets, key frames, audio,
transcript, manifest — so it can be deleted in one go when you are done. The scripts never delete
anything. Options and outputs: see [`video-analysis/SKILL.md`](video-analysis/SKILL.md).

Bundles use schema 2, with paths relative to the `.analysis` folder. A schema-1 `bundle.json` is
refused with exit 3; send that file to the Recycle Bin and rerun `build_bundle.py`. Transient key
frames are not OCR'd and are marked `ocr_skipped: "transient"`.

Online downloads use short safe names: an ASCII lowercase title of up to 40 characters, cut at a
word boundary, followed by `_` and the video ID; the full stem is at most 60 characters. Local
files are never renamed.

Temporary and self-test work goes in the installed skill's `video-analysis/_temp` folder. The whole
`_temp` folder can be sent to the Recycle Bin when no run is in progress; scripts never delete it.
Set `VIDEO_ANALYSIS_TEMP_DIR` to an absolute path to redirect temporary work, e.g. for sandboxed or CI runs.
Analysis outputs stay in `<video>.analysis/` next to the video.

## Status

v0.1.0 — see [RELEASE_NOTES.md](RELEASE_NOTES.md), including the improvement roadmap.

## License

MIT — see [LICENSE](LICENSE).
