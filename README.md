# video-analysis

A [Claude Code](https://claude.com/claude-code) skill that lets an agent **read a local video**:
what is **seen** (sampled frames, timestamped contact sheets, screen changes, exact-time frames) and
what is **said** (offline timestamped transcript), with a procedure to report both cross-referenced
by timestamp — keeping what is on screen, what is said, and what is inferred apart.

Fully offline after a one-time model download. No system ffmpeg, no admin rights: tested on
Windows 11 with Python 3.12.

## Install

```bash
git clone https://github.com/alfibano/video-analysis.git
# make the skill visible to Claude Code (user level), e.g. on Windows:
#   mklink /J "%USERPROFILE%\.claude\skills\video-analysis" "<clone>\video-analysis"
# or copy the video-analysis/ folder into ~/.claude/skills/
python video-analysis/scripts/setup_tools.py        # installs tools, caches the Whisper model
python video-analysis/scripts/selftest.py           # 10 checks on a synthetic video
```

## Use

In Claude Code: "analyse this video: <path>". Or directly:

```bash
python video-analysis/scripts/extract_video.py "<video>" --language fr --interval 5
```

Everything goes to `<video folder>/<video name>.analysis/` — frames, sheets, key frames, audio,
transcript, manifest — so it can be deleted in one go when you are done. The scripts never delete
anything. Options and outputs: see [`video-analysis/SKILL.md`](video-analysis/SKILL.md).

## Status

v0.1.0 — see [RELEASE_NOTES.md](RELEASE_NOTES.md), including the improvement roadmap.

## License

MIT — see [LICENSE](LICENSE).
