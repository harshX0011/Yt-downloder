# Authorized Media Downloader

Paste a video URL, see its real metadata, and download the actual file when the
source publishes one openly.

There is no demo data anywhere in this app. Every title, thumbnail, duration and
file size on screen comes from the source's own endpoint, and the download button
either produces a real file or is not shown at all.

---

## Read this first: YouTube downloads

**There is no official or legal mechanism to turn a YouTube URL into an MP4 file,
so this app does not provide one.**

That is not a limitation of the implementation. It is the state of the platform:

- The **YouTube Data API v3** returns metadata only. It has no endpoint that
  returns a media stream or a file, and neither does any other Google API.
- YouTube's **Terms of Service** prohibit downloading content except through a
  feature YouTube itself provides.
- The only YouTube-sanctioned downloads are **Premium offline** (in the YouTube
  app, for playback inside the app) and **YouTube Studio / Google Takeout** for
  videos on a channel you own. Neither is exposed programmatically.
- Tools that do fetch YouTube streams work by defeating YouTube's signature
  (`n`-parameter) descrambling, rate limiting and bot detection. Those are
  technical protection measures, and working around them is exactly what this
  project will not do.

So for a YouTube link this app does the honest thing:

| What you asked for | What this app does |
| --- | --- |
| Thumbnail, title, duration | Real values from the YouTube Data API v3, or from YouTube's public oEmbed endpoint when no API key is set |
| Playback | The official IFrame player, via `youtube-nocookie.com` |
| MP4 file | Refused, with the reason stated on screen and the official alternatives listed |

`POST /api/download` for a YouTube URL returns **422** with that reason. That
refusal is covered by a test, so it cannot regress.

### What you get instead

A real download pipeline that works end to end on sources that publish their
media openly, with nothing to circumvent:

| Source | Host | Why it is allowed |
| --- | --- | --- |
| Internet Archive | `archive.org` | Public domain and openly licensed collections |
| Wikimedia Commons | `commons.wikimedia.org` | Freely licensed media |
| PeerTube | any PeerTube instance | Open, self-hosted federated video |
| media.ccc.de | `media.ccc.de` | Creative Commons conference recordings |
| TED | `ted.com` | Creative Commons talks TED itself publishes for download |
| Odysee and LBRY | `odysee.com` | An open publishing protocol whose files are meant to be retrieved directly |
| Direct media URL | any public host | A link that points straight at a file, e.g. `https://example.org/talk.mp4` |

The list is configurable through `ALLOWED_EXTRACTORS`. Adding a source is a legal
judgement, so it is yours to make deliberately, not a default.

The UI offers each of these as a one-click example, so you can see a real file
arrive without hunting for a link first.

---

## How it works

```
                    ┌──────────────────────────────┐
   URL ──▶ classify │ youtube? direct file? other? │
                    └──────────────┬───────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  YouTube host              Direct media URL            Everything else
  never reaches             redirect-checked            yt-dlp probe, then
  the extractor             HEAD, then a                the extractor key must
        │                   streaming GET               be on the allowlist
        ▼                          │                          │
  Data API v3 / oEmbed             └────────────┬─────────────┘
  + official embed                              ▼
  + refusal + official             job queue ▶ download ▶ remux to MP4
    alternatives                   SSE progress ▶ file served once ▶ TTL delete
```

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/inspect` | Real metadata, plus whether a download is available and why not |
| `POST` | `/api/download` | Start a download job (202 with a job id) |
| `GET` | `/api/jobs/{id}` | Job status |
| `GET` | `/api/jobs/{id}/events` | Server-sent progress events |
| `GET` | `/api/jobs/{id}/file` | The finished file, as an attachment |
| `DELETE` | `/api/jobs/{id}` | Cancel a running job or release a finished one |
| `GET` | `/api/policy` | The source policy the UI renders |
| `GET` | `/api/health` | Liveness, including whether ffmpeg is present |
| `GET` | `/api/docs` | Interactive OpenAPI docs |

### Layout

```
backend/
  config.py      environment-driven settings, all with safe defaults
  security.py    SSRF guard, filename hygiene, rate limiter
  sources.py     the allowlist policy: what may be downloaded
  youtube.py     official metadata (Data API v3, oEmbed) and the refusal
  extractor.py   metadata probing for allowed sources
  downloader.py  the two real download paths
  jobs.py        job registry, progress, TTL cleanup
  main.py        FastAPI routes, middleware, error translation
frontend/        responsive single page, no build step, strict CSP
tests/           107 tests covering policy, security and the full job lifecycle
vendor/yt-dlp/   the yt-dlp source, vendored for reference only
```

The app imports `yt-dlp` from `requirements.txt`, not from `vendor/`. The vendored
tree is there to read. Its `.github/workflows` are inert, because GitHub Actions
only runs workflows from the repository root.

---

## Run it

### Docker (recommended: ffmpeg is included)

```bash
cp .env.example .env     # optional, add YOUTUBE_API_KEY for durations
docker compose up --build
```

Open <http://localhost:8000>.

### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn backend.main:app --reload
```

Install `ffmpeg` too if you want split audio and video streams merged and remuxed
to MP4. Without it the app still downloads, but only offers single-file
renditions, and `/api/health` reports `"ffmpeg": false`.

### Get a live URL

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/harshX0011/Yt-downloder)

That button reads `render.yaml` and creates a **free** web service. Three steps:

1. Click it and sign in to Render with GitHub.
2. Render prompts for `YOUTUBE_API_KEY`. Paste one from
   [Google Cloud Console](https://console.cloud.google.com/apis/credentials) with
   *YouTube Data API v3* enabled, or leave it blank; the app still runs on
   YouTube's public oEmbed endpoint, just without durations.
3. Apply. The first Docker build takes four to six minutes. Your URL is then
   `https://<service-name>.onrender.com`, and `/api/health` should return
   `{"status":"ok"}`.

Free instances sleep after 15 minutes idle and take about a minute to wake, and
they have 512 MB of RAM with an ephemeral disk, so `render.yaml` caps downloads
at 256 MB and one at a time. Switch `plan` to `starter` for an always-on
instance and raise the limits.

Other hosts:

- **Railway, Fly.io, Heroku-style**: the `Procfile` and `Dockerfile` both work.
- **Anywhere else**: the container listens on `$PORT` and answers `/api/health`.

Behind a reverse proxy, set `TRUST_FORWARDED_FOR=true` so rate limiting sees the
real client IP.

### Configuration

Every variable is documented in `.env.example`. The ones that matter most:

| Variable | Default | Notes |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | unset | Adds duration, views and licence to YouTube previews |
| `ALLOWED_EXTRACTORS` | `ArchiveOrg,Wikimedia,PeerTube,CCC,TedTalk,LBRY` | The download allowlist |
| `MAX_DOWNLOAD_BYTES` | 1 GiB | Enforced while transferring, not just from Content-Length |
| `MAX_CONCURRENT_DOWNLOADS` | 2 | Server-wide |
| `JOB_TTL_SECONDS` | 1800 | Finished files are deleted after this |
| `RATE_LIMIT_REQUESTS` | 30 per 60s | Per client IP |
| `ALLOWED_PORTS` | `80,443` | Origin ports the app will fetch from |
| `ALLOW_PRIVATE_ADDRESSES` | `false` | Leave off. Turning it on disables the SSRF guard |

---

## Security

- **SSRF guard.** Every outbound URL is validated before it is fetched: scheme
  and port allowlists, no embedded credentials, and DNS resolution checked
  against private, loopback, link-local, reserved and IPv4-mapped-IPv6 ranges.
  **Every redirect hop is revalidated**, so a public hostname cannot bounce the
  server onto `169.254.169.254`.
- **No protection circumvention.** yt-dlp runs with `geo_bypass=False`, no cookie
  file, no browser cookie import, and TLS verification on. Its own
  `allowed_extractors` option is pinned to anchored regexes of the allowlist, so
  even a URL that slipped past routing cannot be handled by another extractor.
- **Resource limits.** Size and duration ceilings, a concurrency semaphore, a
  per-IP sliding-window rate limiter, and a TTL reaper that deletes finished files.
- **Output safety.** Job ids are `secrets.token_urlsafe`. Filenames are
  sanitized, including Windows reserved names, and served with RFC 6266
  `Content-Disposition` and `X-Content-Type-Options: nosniff`.
- **Browser hardening.** A strict CSP with no `unsafe-inline`, which is why the
  CSS and JS are separate files. The frontend renders every API value with
  `textContent` and builds links as DOM nodes, so no response field is ever
  treated as markup.
- **Secrets.** `YOUTUBE_API_KEY` is read from the environment, never logged, and
  never echoed in an error response. A test asserts that.

---

## Tests

```bash
pytest -q          # 107 tests
ruff check .
```

Verified end to end during development, in a real browser:

- YouTube link: real metadata rendered, official embed player, download refused
  with the reason and the official alternatives shown.
- Direct media URL: inspect, download, SSE progress, and a saved file that is
  **byte-for-byte identical** to the origin's.
- Size cap, non-media content type, private-address URLs, non-allowlisted sites
  and unknown formats all rejected with clear messages.
- Mobile at 390px wide: zero horizontal overflow. Light and dark both supported.

---

## Licence and responsibility

This project is MIT licensed. `vendor/yt-dlp/` is public domain under the
Unlicense.

Downloading media does not grant you rights to it. Respect each source's licence
and the rights of the people who made the work. Note that yt-dlp's own repository
carries a policy forbidding AI-assisted contributions; that governs contributions
to *their* project, so do not send AI-written patches or issues upstream.
