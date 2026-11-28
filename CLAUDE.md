# CLAUDE.md

@.claude/progression.md

Source of the user-level Claude Code skill `video-digest` (public GitHub repo
`alfibano/video-digest`). The skill itself is the `video-digest/` folder (SKILL.md + scripts/);
the repo root holds README, LICENSE (MIT), RELEASE_NOTES.md (versions + improvement roadmap).

- Python 3.12, numpy, Pillow, imageio-ffmpeg, faster-whisper; no pytest — `scripts/selftest.py` is the
  test suite and must stay green (`python video-digest/scripts/selftest.py [--full]`).
- Runtime environment settings use the `VIDEO_DIGEST_*` prefix. The deprecated
  `VIDEO_ANALYSIS_*` names remain accepted as fallback aliases; when both names are set, the
  `VIDEO_DIGEST_*` value takes precedence, including an empty value.
- Hard rule: every output goes to `<video folder>/<video name>.analysis/`; scripts never delete.
- Public repo: no personal data, paths or emails in files or commit metadata (commits use the GitHub
  noreply address).
- Every release: bump RELEASE_NOTES.md, run the self-test, move done roadmap items into the release.
