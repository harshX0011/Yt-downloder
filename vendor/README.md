# vendor/yt-dlp

A copy of the yt-dlp source tree (version 2026.08.19), kept here for reference.

**The application does not import from this directory.** It installs `yt-dlp`
from `requirements.txt`, and uses it only for the authorized source allowlist in
`backend/sources.py`. See the repository README for the source policy.

## Two things to know

**One file is missing.** `yt_dlp/extractor/shahid.py` is not included. Upstream it
contains hardcoded AWS credentials as module constants for the Shahid extractor,
and GitHub's push protection refuses any push containing them. Nothing here
depends on that file. To restore it, take it from
<https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/shahid.py> and
allow the secrets through the link GitHub shows when the push is blocked.

**The workflows here are inert.** GitHub Actions only runs workflows found in the
repository's root `.github/workflows/`, so `vendor/yt-dlp/.github/workflows/`
never executes. Its nightly release and wiki jobs will not run in this repository.

## Upstream policy

yt-dlp's own repository forbids AI-assisted contributions (see
`yt-dlp/.NO_AI/README.md`). That governs contributions to *their* project. Do not
send AI-written patches, issues or review comments upstream.

yt-dlp is released into the public domain under the Unlicense; see
`yt-dlp/LICENSE`.
