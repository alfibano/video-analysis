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
| contact sheets are opt-in | --contact-sheets creates sheets; default extraction omits them and can resume existing frames |
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
| correction provider selection matrix | CLI/environment/host/key precedence and defaults resolve offline |
| correction CLI adapters are offline and account usage | fake runners assert commands, stdin, timeout, parsing, and normalized usage |
| correction prose refusals are retryable | explicit refusal prose is retryable; valid numbered lines are not |
| correction retry sends only refused chunks | retry configuration selects only recorded refused chunks |
| correction consent EOF and noninteractive stdout exit 4 | EOF and redirected output deny without constructing a provider |
| bundle uses matching corrected speech provenance | a matching correction is selected and provenance is recorded |
| bundle ignores stale corrected speech | mismatched corrections fall back to original speech |
| bundle resume keys selected speech input | changed corrected speech input conflicts with bundle resume |
| setup reports correction readiness without secrets | setup reports SDK/key readiness without exposing key values |
| analyze_video correction order and flags | the pipeline runs correction in order and forwards its options |
| documentation contract current | README and skill disclose correction data flow, consent, flags, host locality, and release state |
| every script help describes itself | each command's help has valid sections, usage detail and exit codes |
| *(with `--full`)* wav is 16 kHz mono PCM16 | the audio is in the format Whisper expects |
| *(with `--full`)* transcript files written | transcription runs (no `metadata_errors` crash) |

## v0.6.0 sheet checks

Study-sheet fixtures are generated under `VIDEO_ANALYSIS_TEMP_DIR`: fake video and bundle/manifest,
a tiny PNG, valid `sheet.json`, and temporary `.docx`/`.dotx` templates. Tests use a fake
`ConfigReader`; they read no real host configuration and call no provider, executable, or network.
The DOCX checks inspect paragraph order and styles, image count, core properties, hyperlink
relationships, template headers/footers/sections, and `.dotx` content types. Package byte identity is
not required because ZIP metadata and relationship ordering vary.

| Group | Named checks |
|---|---|
| Schema | `sheet schema accepts valid v2`; strict nested v2, duration summary, frames, references, context evidence, safe URLs, and callout budget checks |
| Facts and accounting | deterministic sourced facts; measured correction plus estimated writing tokens; incomplete or unknown USD is `n/a` |
| Comments | bounded top-level collection, identity removal, opt-out, unavailable state, and independent resume |
| Preparation and consent | `sheet preparation uses host default model provenance`; `sheet preparation consent matrix enforces write boundaries`; `sheet brief embeds facts and the complete editorial contract`; `sheet preparation resumes and conflicts safely` |
| Rendering and templates | `default DOCX renders facts accounting context blocks callouts and references`; facts formatting, omitted empty/duplicate facts, one-row description links, localized cost labels, schema-v1 refusal, hyperlinks, tables, borders, shading, template preservation |
| Resume safety | `sheet render rerun preserves user-edited DOCX`; `changed sheet rerenders owned DOCX atomically and preserves edited DOCX` (recorded output SHA-256, same-name replacement, temp cleanup, and replacement failure); `untracked DOCX is refused`; `sheet render requires successful preparation`; `sheet render rejects changed bundle digest` |
| Setup | `setup maps docx import to python-docx` |
| Extraction | `key-sheet pages reuse matching contact sheets and are indexed`; verifies ordered page reuse and SHA-256 uniqueness across `keysheets/` and `sheets/` |
| Cleanup | `selftest cleanup removes tracked folders and refuses outside paths`; verifies nested owned folders under a `TemporaryDirectory` are removed and an outside path triggers the guard |
| Documentation | `v0.6.5 documentation performance and measured-count contract` |

The check named `default DOCX render has ordered content images and properties` also verifies that
built-in-template hyperlink runs have visible formatting when no `Hyperlink` character style exists.
Source links must match `bundle.json` and use a safe HTTP(S) URL; unsafe values remain plain text.
Measured release totals: `DEFAULT_CHECKS=145` (145 passed, 0 failed); `FULL_CHECKS=147`
(147 passed, 0 failed; two full-mode transcription checks add to the default total).
New v0.6.5 offline checks are named exactly: caption language expansion and fetch resume are lossless; usable captions create transcript and force-Whisper bypasses them; automatic caption transcript remains correctable; Whisper CUDA success records actual device; Whisper load failure falls back to CPU; Whisper iteration failure discards partial and falls back; CUDA PATH helper prepends existing NVIDIA bins once; setup installs GPU speech libraries only with a GPU; legacy extraction manifest resumes with default transcribe and device; contact sheets are opt-in and resume from completed frames; analyze_video forwards contact sheets only to extraction.

On a green run, the self-test removes only tracked folders it created inside the app temp root and prints
`test folders removed`. When any check fails, it prints `test folders retained for diagnosis` followed
by their paths. `key_sheet_index` in the extraction manifest points readers to every key page, including
pages reused from the contact sheets.

The two refusal checks matter: a guard that has never been seen refusing proves nothing.

## Correction provider checks

Offline correction checks cover provider selection, Claude/Codex command arguments and usage parsing,
prose refusal classification, refused-only retries, and consent EOF/noninteractive behavior. Fake
subprocess runners assert stdin prompt delivery and prevent any real provider or CLI invocation.
Setup and pipeline checks also cover readiness without secrets, provider flags, and documentation.

## When to run it

Normal setup runs this suite after readiness. Run setup_tools.py --check to run only the offline
self-test without installation or readiness probes.

- **After installing** on a new machine, right after `setup_tools.py`.
- **Before every release**, to make sure an improvement broke nothing.

## Usage

```bash
python video-analysis/scripts/selftest.py            # fast, no transcription (no model needed)
python video-analysis/scripts/selftest.py --full     # requires a cached Whisper model
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
