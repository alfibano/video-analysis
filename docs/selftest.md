# Self-test — `selftest.py`

`video-analysis/scripts/selftest.py` checks the skill without a real video. The project has no
pytest dependency: this script **is** its test suite. Its default run never imports `yt_dlp` or
opens a network connection or calls an AI provider; correction coverage uses injected fake clients.
A real URL run is explicit with `--network URL`. The default self-test makes zero provider calls.

## What it does

1. Builds a 20-second synthetic video: two static shots of different colours (cut at 10 s) with a
   440 Hz tone.
2. Runs `extract_video.py` on it several times.
3. Prints `PASS` or `FAIL` for each check, then the folder holding its test files.

| Check | What it proves |
|---|---|
| first run exit 0 | the pipeline runs end to end |
| folder created next to the video | the working folder lands beside the video, nowhere else |
| adaptive base grid and change margin | adaptive sampling keeps the base grid and one-second window around a scene change |
| adaptive sampling and timestamp names | sampled JPEG names use rounded PTS milliseconds and remain in timestamp order |
| fixed interval opt out | --fixed-interval keeps the five-second grid and sampling settings participate in resume checks |
| 1 contact sheet | contact sheets are built |
| scene change detected (2 key frames) | key-frame detection keeps changes and drops duplicates |
| exact frame at 00:00:12.250 | `--at` extracts a frame at an exact instant |
| audio detected | the audio stream is found |
| rerun exit 0 and frames untouched | a rerun resumes without redoing finished work |
| changed parameters refused with exit 3 | a guard refuses to mix outputs made with other settings |
| non-empty folder without manifest refused with exit 3 | a guard refuses to adopt a folder it did not create |
| build bundle writes JSON from extract outputs | chapter coverage, frame PTS and transcript provenance make it into `bundle.json` |
| bundle paths are relative to the analysis folder | source, speech and frame files resolve without absolute paths |
| downloaded video names are short and safe | title and ID produce an ASCII stem no longer than 60 characters |
| chapter source precedence and bounds | metadata chapters and fallback chapters follow the configured duration limits |
| bundle works without fetch outputs | local extraction data is sufficient when fetch metadata and captions are absent |
| bundle CLI handles SponsorBlock segments | yt-dlp SponsorBlock records become sponsor flags on overlapping speech and frames, appear in chapter overlap, and are excluded from summary-ready speech |
| bundle with fake fetch outputs prefers manual subtitles | manual language captions take priority over automatic captions |
| subtitle priority and rolling VTT dedupe | subtitle sources are ordered and repeated rolling-window cues are deduplicated |
| srt and vtt normalize to segments | both subtitle formats produce normalized timed speech segments |
| bundle manifest resumes and conflicts safely | identical settings reuse output; changed settings exit 3 |
| extract preserves bundle manifest params | later extraction keeps `params.bundle` and its stage |
| frames align by exact PTS | frame assignment uses the stored presentation timestamps |
| sponsor spans remain flagged and summary text excludes them | sponsor overlap is retained as metadata and omitted from summary-ready speech |
| OCR runs on key frames only | OCR input is limited to detected key frames |
| OCR adapter is lazy | OCR dependencies load only when OCR is requested |
| OCR readiness reported by setup | setup reports a RapidOCR version or a clear missing state |
| OCR uses bounded ONNX threads and reports progress | CPU thread selection is clamped and OCR progress follows terminal/redirected output policy |
| OCR runtime thread count does not invalidate bundle resume | changing only the ONNX thread setting resumes the existing bundle |
| transient key frames are not OCR'd | adaptive key frames followed within the sampling window are marked and omitted from OCR |
| analyze_video local and URL pipelines | local input extracts then bundles; URL input fetches, extracts, and bundles through mocked steps |
| analyze_video stops on failure | the pipeline stops at the first failed stage and returns its exit code |
| setup installs then tests and check only tests | normal setup runs the offline self-test after readiness; --check runs only that test |
| fetch progress is bounded by output mode | yt-dlp progress hooks report bounded percentage or unknown-size progress |
| offset container PTS preserved | timestamp names and `frames.json` preserve a nonzero container offset |
| real OCR smoke | generated `Résumé` text is recognized, or reports `SKIP` when RapidOCR is unavailable |
| fetch manifest accepted by extract | v2 hand-off retains fetch data while extract records its own settings |
| subtitle failure does not fail the fetch | a fake yt-dlp raises during the subtitle pass; video and metadata remain available and the missing language is recorded |
| v0.1 manifest remains compatible | older flat manifests are migrated to v2 in memory |
| playlist and live are refused before download | a fake yt-dlp confirms preflight happens before downloading |
| temporary folders live in the app _temp folder | test runs use the skill's `_temp` folder or `VIDEO_ANALYSIS_TEMP_DIR`, and scripts centralize temporary folder creation |
| default selftest is offline | default checks keep yt-dlp lazy and do not call the network |
| documentation contract current | usage docs mention fetch, tool checks, network self-test and `frames.json` |
| manifest records local and URL source kinds | media provenance is explicit before consent evaluation |
| extract preserves correction manifest state | extraction keeps correction settings and stage records |
| correction contract parses exact numbered lines | response IDs are complete, unique, and have no extra prose |
| correction guard rejects paraphrase and omission | unsafe edits keep the original text |
| correction refusal and malformed chunks keep originals | refusal and malformed output safely fall back |
| correction DRY shared source and speech helpers | correction and bundle use the same source selection and normalization |
| correction consent matrix blocks before provider call | remote correction for local media requires consent; URL and declared local endpoints do not |
| correction skip modes stay offline | opt-out and missing remote API key skip without constructing a provider |
| manual subtitles skip correction unchanged | manual captions retain precedence and are not modified |
| correction resume and conflict are safe | matching results resume; changed settings exit 3 |
| correction fake client writes validated output | fake responses write the versioned corrected transcript and usage |
| correction calls are bounded and usage is recorded | at most two requests run at once and token totals are recorded |
| correction provider errors fall back per chunk | provider failures preserve originals and report the error type |
| correction local endpoint runs without API key | declared local endpoint receives a fixed placeholder key |
| bundle uses matching corrected speech provenance | a matching correction is selected and provenance is recorded |
| bundle ignores stale corrected speech | mismatched corrections fall back to original speech |
| bundle resume keys selected speech input | changed corrected speech input conflicts with bundle resume |
| setup reports correction readiness without secrets | setup reports SDK/key readiness without exposing key values |
| analyze_video correction order and flags | the pipeline runs correction in order and forwards its options |
| documentation contract current | README and skill disclose correction data flow, consent, flags, host locality, and release state |
| every script help describes itself | each command's help has valid sections, usage detail and exit codes |
| *(with `--full`)* wav is 16 kHz mono PCM16 | the audio is in the format Whisper expects |
| *(with `--full`)* transcript files written | transcription runs (no `metadata_errors` crash) |

The two refusal checks matter: a guard that has never been seen refusing proves nothing.

## When to run it

Normal setup runs this suite after readiness. Run setup_tools.py --check to run only the offline
self-test without installation or readiness probes.

- **After installing** on a new machine, right after `setup_tools.py`.
- **Before every release**, to make sure an improvement broke nothing.

## Usage

```bash
python video-analysis/scripts/selftest.py            # fast, no transcription (no model needed)
python video-analysis/scripts/selftest.py --full     # also runs Whisper (model downloaded once)
python video-analysis/scripts/selftest.py --work-dir <folder>   # choose where test files go
python video-analysis/scripts/selftest.py --network <URL>       # also fetch and extract a real URL
```

The default suite uses fake OCR and correction clients for orchestration and runs a real OCR smoke
when RapidOCR is installed. It makes zero provider calls. The OCR smoke reports `SKIP` when the
package is unavailable; it does not fail the suite.

Exit code `0` = every check passed, `1` = at least one failed.
Network mode writes its download under `<work-dir>/network` (or the fresh test folder).

## Cleanup

Test files go to a fresh folder in the installed skill's `video-analysis/_temp` folder (or `--work-dir`),
printed at the end. The whole `_temp` folder can be sent to the Recycle Bin when no run is in progress;
the script never deletes anything. Analysis outputs remain in `<video>.analysis/` next to the video.
Set `VIDEO_ANALYSIS_TEMP_DIR` to an absolute path to redirect temporary work, e.g. for sandboxed or CI runs.
