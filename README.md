# YoutubeAutoUploader

Record a class, and by the time you're back at your desk the lecture is on
YouTube — remuxed, transcribed, captioned, titled, thumbnailed, added to the
right playlist, and listed on a static index page.

Built for a specific workflow (OBS on macOS, one instructor, a handful of
courses a semester) and published because the awkward parts — the HEVC tag
QuickTime needs, the OAuth scope captions actually require, the way OneDrive
placeholder files fail — took a while to work out and are not well documented
anywhere.

## Pipeline

1. `obs_watcher.py` runs under `launchd` and watches the OBS recordings folder
2. A new `.mkv` appears; the script waits for OBS to finish writing it
3. A cancel window opens — delete the file during it to abort the upload
4. Remux to `.mp4` (`+faststart`, plus the `hvc1` tag when the source is HEVC)
5. Transcribe with `whisper.cpp`, writing `.srt`/`.txt`/`.vtt` to a transcript
   archive organized by semester and course
6. Match the recording timestamp against the course schedule to derive the
   title, description, and playlist
7. Upload unlisted, category Education, with the recording date set
8. Add to the playlist, set the thumbnail, upload the captions
9. Regenerate that course's index page and push it to the site
10. Send a notification with the video link

Uploads only run on a wired connection. On Wi-Fi the file is queued and
retried every five minutes.

Companion scripts:

- `manual_upload.py` — interactive uploader for recordings that don't match a
  scheduled class (a one-off extra video, a class that ran at an odd time)
- `generate_lecture_index.py` — builds a static HTML index page per course
  from its YouTube playlist, for embedding in an LMS where YouTube's own
  playlist panel no longer renders reliably
- `test_notifications.py` — fires one of each notification type
- `benchmark_whisper.py` — times whisper models against a real recording

## Requirements

- macOS (uses `launchd`, `networksetup`, `route`, and the `security` keychain
  tool)
- Python 3.11+
- `ffmpeg` and `whisper-cpp` — `brew install ffmpeg whisper-cpp`
- A whisper model, e.g.
  [`ggml-medium.en.bin`](https://huggingface.co/ggerganov/whisper.cpp), placed
  in `models/`
- A Google Cloud project with the YouTube Data API v3 enabled
- Optional: a [Pushover](https://pushover.net) account for notifications
- Optional: a static site you can `git push` to, for the index pages

```bash
python3 -m pip install watchdog google-auth google-auth-oauthlib \
    google-api-python-client
```

Install against the same interpreter the launchd agent will run. Homebrew's
`python3` is not `/usr/bin/python3`, and installing into the wrong one
produces `ModuleNotFoundError` at startup with no other symptom.

## Setup

### Configuration

Every machine-specific value lives in one gitignored file:

```bash
cp config.example.py config.py
```

Edit `config.py` and set `WATCH_FOLDER`, `TRANSCRIPT_ROOT`, `INSTITUTION`, and
— if you want index pages — `SITE_REPO`. The binary paths assume Homebrew on
Apple Silicon; adjust for Intel macOS (`/usr/local/bin`) or Linux.

`SCRIPT_DIR` resolves itself and needs no editing. Every other path is
derived from it, so the repository runs wherever you clone it. All four
scripts read this one file; nothing else needs changing.

A missing or incomplete `config.py` fails at import with a message naming the
missing value, not a traceback forty minutes later mid-upload.

### OAuth

Create a Google Cloud project, enable **YouTube Data API v3**, and create an
OAuth client of type **Desktop app**. Save the downloaded JSON as
`client_secrets.json` in the repo directory.

The scopes matter:

```python
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
```

`youtube` alone is enough to upload videos and manage playlists, but
`captions.insert` fails with `insufficientPermissions` without `force-ssl`.
Changing the scope list invalidates an existing token — delete `token.json`
and re-authorize. A running process holds the old list in memory, so restart
it after editing or you'll get `invalid_scope` instead.

Publish the OAuth consent screen to **Production**. Left in Testing, refresh
tokens expire after seven days, which means re-authorizing every week.

### Course schedule

Copy `courses/courses.example.json` to `courses/courses-<SEMESTER>.json`, fill
in your own courses, and symlink it:

```bash
ln -sfn courses/courses-FA26.json courses.json
```

Recordings are matched by date range, day of week, and time window (with a
grace period before the start time, since recording usually begins before
class does). A recording that matches nothing is skipped rather than uploaded
with a wrong title.

Only `name` is required. `start_date`, `end_date`, and `meetings` are needed
for schedule matching; `thumbnail` for custom thumbnails; and `title`,
`page_slug`, and `newest_first` are optional keys the index generator honors.

Archived semesters stay in `courses/` and can be regenerated later with
`--semester`. They do not need the scheduling keys if you only want their
index pages.

### Notifications (optional)

Credentials live in the macOS keychain, not in source. Only the keychain
*service names* appear in `config.py`:

```bash
security add-generic-password -a "$USER" -s pushover_obs_uploader_token -w
security add-generic-password -a "$USER" -s pushover_user -w
```

`test_notifications.py` fires one of each notification type so you can confirm
delivery and priority behavior without recording anything.

Set `NOTIFY_ENABLED = False` to skip this entirely; nothing else depends on it.

Two things about iOS: the keychain must be unlocked, so these lookups fail in
an SSH session even though they work fine for a GUI-launched process. And
Focus modes sit above Pushover's own priorities — priority 1 maps to Time
Sensitive, which each Focus has to allow separately.

### Running under launchd

Copy `com.example.obsrecordingwatcher.plist.example`, edit the paths and
label, then:

```bash
mkdir -p logs   # launchd will not create this, and fails silently without it
cp com.<you>.obsrecordingwatcher.plist ~/Library/LaunchAgents/
chmod 644 ~/Library/LaunchAgents/com.<you>.obsrecordingwatcher.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<you>.obsrecordingwatcher.plist
```

Point `StandardOutPath` and `StandardErrorPath` at files *other* than the
logger's own `logs/obs_watcher.log` — otherwise every line is written twice,
once by the logger and once by launchd redirecting stdout into the same place.

Keep the scripts outside `~/Documents`, `~/Desktop`, and `~/Downloads`.
Processes launched by `launchd` don't have Full Disk Access by default and
will fail with `Operation not permitted` in those directories.

After editing a script, restart the process (`pkill -f obs_watcher.py`;
`KeepAlive` brings it back). Only plist changes need `bootout`/`bootstrap`.

## Usage

Normal operation is hands-off. The manual paths:

```bash
# Upload a recording that matched no scheduled class
python3 manual_upload.py
python3 manual_upload.py --all          # list candidates, upload nothing
python3 manual_upload.py --dry-run      # stop at the summary, write nothing

# Rebuild index pages
python3 generate_lecture_index.py --all --dry-run
python3 generate_lecture_index.py --course "MAT 101A" --push
python3 generate_lecture_index.py --all --config        # + robots.txt, _headers
python3 generate_lecture_index.py --semester SP26 --all # an archived term

# Compare whisper models on a real recording before committing to one
python3 benchmark_whisper.py --model medium.en /path/to/lecture.mp4
```

The log rotates at 5 MB, keeping three backups. Follow it with `tail -F`
rather than `tail -f`, so it survives a rotation:

```bash
tail -F logs/obs_watcher.log
```

## Notes from the build

Things that cost time to work out:

**QuickTime and HEVC.** A straight remux produces an MP4 QuickTime opens but
plays with no video. HEVC has to be tagged `hvc1`, not the `hev1` that ffmpeg
writes by default. The remux probes the source codec first, because the tag is
invalid on H.264 and ffmpeg refuses the whole operation — which is exactly what
happened when an OBS update silently reset the encoder.

**Watch for the file to stop growing.** `watchdog` fires on file *creation*,
which for a recording is the moment OBS opens the file, not when it finishes.
Handing that to ffmpeg gets `EBML header parsing failed`. Poll the size until
it is stable.

**FSEvents fires twice.** macOS can deliver the same creation event more than
once, and the second one arrives after the first upload completes — so the
same lecture uploads twice under different video IDs. A set of in-flight paths
handles it.

**Don't re-queue after a successful upload.** If the playlist or thumbnail
step throws, the video is already public; returning failure makes the retry
loop upload it again. Only the video upload itself decides success. A third
state distinguishes "deliberately skipped" from "failed, retry later", so an
unmatched recording doesn't retry forever.

**`SystemExit` isn't an `Exception`.** The post-upload steps are individually
wrapped in `except Exception` so a late failure can't trigger a re-upload. A
module that calls `sys.exit()` at import time slips straight past that and
takes the watcher down with it — after the video is already live. Modules
imported by the pipeline raise `ImportError` instead.

**OneDrive thumbnails fail intermittently.** A Files On-Demand placeholder
passes `Path.exists()` but raises `Errno 11: Resource deadlock avoided` when
actually opened. Keep thumbnails on local disk.

**Quota.** An upload costs about 2,100 units of the default 10,000/day:
`videos.insert` 1,600, `captions.insert` 400, `playlistItems.insert` 50,
`thumbnails.set` 50. Roughly four or five lectures a day. Extensions are free
but require review.

**New playlists aren't immediately writable.** Inserting into a playlist right
after creating it can return `SERVICE_UNAVAILABLE`. Retry with backoff — it
only bites on the first upload of each course each term.

**Whisper model choice is not just a speed tradeoff.** On an M4 Max,
`small.en` transcribes a 47-minute lecture in 28 seconds against `medium.en`'s
66 — but it mangles domain vocabulary badly enough to make the captions worse
than none. A minute is cheap; run the bigger model.

**What the API can't do.** Comments can't be disabled per-video (set a channel
default instead), playlist sort order isn't exposed, and the educational
metadata fields in Studio — type, level, academic system — have no API
equivalent.

## License

MIT
