#!/usr/bin/env python3
"""
OBS Recording Watcher + YouTube Auto-Uploader

Pipeline:
  1. Watch the OBS recordings folder for new .mkv files
  2. Wait for OBS to finish writing
  3. Cancel window — delete the .mkv during this period to abort
  4. Remux to .mp4 (QuickTime-compatible HEVC tag + faststart)
  5. Transcribe with whisper.cpp -> .srt/.txt/.vtt under TRANSCRIPT_ROOT
  6. Upload to YouTube with metadata derived from the recording timestamp
     and the course schedule in courses.json
  7. Add to playlist, set thumbnail, upload captions, refresh the index

Recordings made on a date listed in courses.json's "no_class_dates" are
held: left in place, never uploaded, and reported once by notification.
Publish them by hand with manual_upload.py if you want them up.

Machine-specific settings live in config.py — copy config.example.py to
config.py and edit it before first run.

Uploads are only attempted on a wired connection. On Wi-Fi the file is
queued and retried every RETRY_INTERVAL_SECONDS.

Usage:
    python3 obs_watcher.py            # run the watcher (what launchd does)
    python3 obs_watcher.py --dry-run  # report which existing recordings
                                      # would be held; uploads nothing
"""

import re
import sys
import time
import json
import logging
import argparse
import subprocess
import threading
from typing import Any
from datetime import date, datetime, timedelta
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from logging.handlers import RotatingFileHandler


# ============================================================
# CONFIGURATION
# ============================================================
try:
    from config import (
        SCRIPT_DIR,
        WATCH_FOLDER,
        TRANSCRIPT_ROOT,
        LOG_DIR,
        FFPROBE,
        FFMPEG,
        WHISPER,
        WHISPER_MODEL,
        WHISPER_THREADS,
        TRANSCRIBE_ENABLED,
        CAPTION_LANGUAGE,
        PRIVACY_STATUS,
        INSTITUTION,
        DESCRIPTION_TEMPLATE,
        PRE_CLASS_GRACE_MINUTES,
        UPLOAD_DELAY_MINUTES,
        RETRY_INTERVAL_SECONDS,
        NOTIFY_ENABLED,
        PUSHOVER_API_URL,
        PUSHOVER_TOKEN_SERVICE,
        PUSHOVER_USER_SERVICE,
    )
except ImportError as e:
    sys.exit(
        f"Configuration error: {e}\n"
        f"Copy config.example.py to config.py and edit it for this machine."
    )

# --- Derived from SCRIPT_DIR; identical for every install ----
COURSES_FILE = SCRIPT_DIR / "courses.json"
CLIENT_SECRETS_FILE = SCRIPT_DIR / "client_secrets.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
QUEUE_FILE = SCRIPT_DIR / "upload_queue.json"

LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "obs_watcher.log"

# Sentinel: matched no scheduled course, or fell on a no-class date.
# Both are deliberate, so the queue treats this like success and the
# recording isn't retried forever.
SKIPPED = "skipped"

# Top-level key in courses.json listing the dates with no class.
# sync_teaching_focus.py reads the same key, so the name and the ISO
# YYYY-MM-DD format are a shared contract — don't rename or reformat.
NO_CLASS_DATES_KEY = "no_class_dates"

# --- YouTube constants; identical for every install ----------
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
CATEGORY_ID_EDUCATION = "27"


# ============================================================
# LOGGING
# ============================================================
log = logging.getLogger("obs_watcher")
log.setLevel(logging.INFO)
log.propagate = False
log.handlers.clear()

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(_formatter)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_formatter)
log.addHandler(_fh)
log.addHandler(_sh)

# ============================================================
# PUSH NOTIFICATIONS (Pushover)
# ============================================================
def keychain_get(service: str) -> str | None:
    """Read a generic password from the login keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception as e:
        log.warning(f"Keychain lookup failed for {service}: {e}")
        return None


# Read once at startup rather than shelling out on every notification
PUSHOVER_TOKEN = keychain_get(PUSHOVER_TOKEN_SERVICE) if NOTIFY_ENABLED else None
PUSHOVER_USER = keychain_get(PUSHOVER_USER_SERVICE) if NOTIFY_ENABLED else None

if NOTIFY_ENABLED and not (PUSHOVER_TOKEN and PUSHOVER_USER):
    log.warning("Pushover credentials not found in keychain; notifications disabled.")


# --- Notification de-duplication ----------------------------
# A queued item is retried every RETRY_INTERVAL_SECONDS, and these alerts
# are priority 1 — Time Sensitive on iOS, so they bypass quiet hours.
# Without these guards an overnight failure sends the same alert a
# hundred times.
#
# notified_paths holds per-recording problems (upload failed, no course
# matched); an entry is cleared once that path uploads successfully.
notified_paths: set[str] = set()

# A malformed courses.json affects every queued item at once, so this is
# one flag rather than per-path. Cleared after any successful upload,
# which proves the file is readable again.
config_error_notified = False

# courses.json is re-read on every lookup so edits take effect without a
# restart — which also means a missing or malformed no_class_dates key
# would otherwise be reported on every single recording.
no_class_missing_notified = False
no_class_invalid_notified = False


def notify(message: str, title: str = "OBS Watcher", priority: int = 0):
    """
    Send a Pushover notification. Never raises — a notification failure
    must not interrupt the pipeline.

    priority: -2 silent, -1 quiet (no sound), 0 normal, 1 high (bypasses
    quiet hours). Emergency (2) is deliberately unused; it requires
    acknowledgement and is overkill here.
    """
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "10",
             "--form-string", f"token={PUSHOVER_TOKEN}",
             "--form-string", f"user={PUSHOVER_USER}",
             "--form-string", f"title={title}",
             "--form-string", f"message={message}",
             "--form-string", f"priority={priority}",
             PUSHOVER_API_URL],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.warning(f"Pushover curl failed (exit {result.returncode})")
        elif '"status":1' not in result.stdout:
            log.warning(f"Pushover rejected the message: {result.stdout[:200]}")
    except subprocess.TimeoutExpired:
        log.warning("Pushover request timed out")
    except Exception as e:
        log.warning(f"Notification failed: {e}")

# ============================================================
# NETWORK CHECK
# ============================================================
def is_wired_connection() -> bool:
    """True if the default route is NOT through the Wi-Fi interface."""
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, check=True,
        )
        wifi_iface = None
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line:
                for j in range(i, min(i + 4, len(lines))):
                    if "Device:" in lines[j]:
                        wifi_iface = lines[j].split(":", 1)[1].strip()
                        break
                break

        result = subprocess.run(
            ["route", "get", "default"],
            capture_output=True, text=True, check=True,
        )
        default_iface = None
        for line in result.stdout.splitlines():
            if "interface:" in line:
                default_iface = line.split(":", 1)[1].strip()
                break

        if not default_iface:
            return False
        if wifi_iface and default_iface == wifi_iface:
            return False
        return True
    except Exception as e:
        log.warning(f"Network check failed: {e}")
        return False


# ============================================================
# COURSE LOOKUP
# ============================================================
def load_courses() -> dict[str, Any]:
    with open(COURSES_FILE) as f:
        return json.load(f)


def parse_no_class_dates(data: dict[str, Any],
                         notify_problems: bool = True) -> set[date]:
    """
    Extract and validate courses.json's no_class_dates list.

    Returns a set of datetime.date. The key is optional: a missing one
    logs a warning, notifies once, and yields an empty set rather than
    raising, so a schedule written before this feature existed still runs.

    Malformed entries are logged at ERROR and skipped, but the valid
    remainder is still honored — one bad string shouldn't quietly turn
    every day off back into an upload. Dates must be exactly YYYY-MM-DD;
    "2026-1-5" is rejected, because sync_teaching_focus.py reads the same
    key and the format is a shared contract.

    notify_problems=False suppresses the Pushover sends, for dry runs.
    """
    global no_class_missing_notified, no_class_invalid_notified

    raw = data.get(NO_CLASS_DATES_KEY)

    if raw is None:
        if not no_class_missing_notified:
            no_class_missing_notified = True
            log.warning(
                f"courses.json has no '{NO_CLASS_DATES_KEY}' key — no "
                f"recordings will be held. Add it as a list of ISO dates, "
                f'e.g. "{NO_CLASS_DATES_KEY}": ["2026-11-26"].'
            )
            if notify_problems:
                notify(
                    f"courses.json has no '{NO_CLASS_DATES_KEY}' key; "
                    f"day-off holds are inactive.",
                    title="Config Warning", priority=0,
                )
        return set()

    if not isinstance(raw, list):
        log.error(
            f"courses.json '{NO_CLASS_DATES_KEY}' must be a list of "
            f"YYYY-MM-DD strings, got {type(raw).__name__}. Treating it as "
            f"empty — no recordings will be held."
        )
        if notify_problems and not no_class_invalid_notified:
            no_class_invalid_notified = True
            notify(
                f"'{NO_CLASS_DATES_KEY}' in courses.json is not a list; "
                f"day-off holds are inactive.",
                title="Config Error", priority=1,
            )
        return set()

    valid: set[date] = set()
    invalid: list[str] = []

    for entry in raw:
        if not isinstance(entry, str):
            invalid.append(repr(entry))
            continue
        try:
            parsed = datetime.strptime(entry, "%Y-%m-%d").date()
        except ValueError:
            invalid.append(entry)
            continue
        # strptime accepts "2026-1-5"; the round trip rejects it, so what
        # we store always matches what the other consumer expects.
        if parsed.isoformat() != entry:
            invalid.append(entry)
            continue
        valid.add(parsed)

    if invalid:
        log.error(
            f"Ignoring {len(invalid)} malformed "
            f"{'entry' if len(invalid) == 1 else 'entries'} in "
            f"'{NO_CLASS_DATES_KEY}' (expected YYYY-MM-DD): "
            f"{', '.join(invalid)}"
        )
        if notify_problems and not no_class_invalid_notified:
            no_class_invalid_notified = True
            notify(
                f"{len(invalid)} malformed date(s) in "
                f"'{NO_CLASS_DATES_KEY}': {', '.join(invalid[:3])}"
                f"{' and more' if len(invalid) > 3 else ''}",
                title="Config Error", priority=1,
            )

    return valid


def is_day_off(recorded_at: datetime, notify_problems: bool = True) -> bool:
    """
    True if this recording falls on a no-class date.

    recorded_at comes from the OBS filename, which OBS writes in local
    time, so .date() is already the local calendar date — no timezone
    conversion is wanted or applied.

    Fails open: if courses.json can't be read this returns False and the
    recording follows the normal path, where the same unreadable file
    produces a louder error.
    """
    try:
        data = load_courses()
    except Exception as e:
        log.error(f"Could not read courses.json for the day-off check: {e}")
        return False
    return recorded_at.date() in parse_no_class_dates(
        data, notify_problems=notify_problems
    )


def determine_course(recorded_at: datetime) -> tuple[str | None, str | None, str | None]:
    """
    Return (course_name, semester, thumbnail_path) for a recording timestamp,
    or (None, semester, None) if no scheduled class matches.
    """
    data = load_courses()
    semester = data.get("semester")
    day_name = recorded_at.strftime("%A")
    date_str = recorded_at.strftime("%Y-%m-%d")

    for course in data.get("courses", []):
        if not (course["start_date"] <= date_str <= course["end_date"]):
            continue
        for meeting in course.get("meetings", []):
            if meeting["day"] != day_name:
                continue
            start = datetime.strptime(meeting["start"], "%H:%M").time()
            end = datetime.strptime(meeting["end"], "%H:%M").time()
            start_dt = datetime.combine(recorded_at.date(), start)
            end_dt = datetime.combine(recorded_at.date(), end)
            grace = timedelta(minutes=PRE_CLASS_GRACE_MINUTES)
            if (start_dt - grace) <= recorded_at <= end_dt:
                return course["name"], semester, course.get("thumbnail")

    return None, semester, None


# ============================================================
# FILENAME PARSING
# ============================================================
FILENAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})")


def parse_recording_datetime(path: Path) -> datetime | None:
    m = FILENAME_RE.search(path.name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, s)


def sanitize_for_path(name: str) -> str:
    """Strip characters that misbehave in filenames."""
    return re.sub(r'[/\\:*?"<>|]', "-", name).strip()


# ============================================================
# QUEUE
# ============================================================
queue_lock = threading.Lock()


def queue_load() -> list[str]:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        log.warning("Queue file was unreadable; starting fresh.")
        return []


def queue_save(q: list[str]):
    with open(QUEUE_FILE, "w") as f:
        json.dump(q, f, indent=2)


def queue_add(path: str):
    with queue_lock:
        q = queue_load()
        if path not in q:
            q.append(path)
            queue_save(q)
            log.info(f"Queued for later upload: {path}")


def queue_remove(path: str):
    with queue_lock:
        q = queue_load()
        if path in q:
            q.remove(path)
            queue_save(q)


# ============================================================
# FILE STABILITY + CANCEL WINDOW
# ============================================================
def wait_for_file_stable(path: Path, interval: int = 2, stable_checks: int = 3):
    """Block until the file size stops changing."""
    last = -1
    stable = 0
    while stable < stable_checks:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(interval)
            continue
        if size == last:
            stable += 1
        else:
            stable = 0
            last = size
        time.sleep(interval)


def wait_delay_with_cancel(path: Path, delay_minutes: int) -> bool:
    """
    Wait `delay_minutes`, polling for the file's continued existence.
    Returns True if the window elapsed and the file is still present,
    False if it was deleted (upload cancelled).
    """
    if delay_minutes <= 0:
        return True

    delay_seconds = delay_minutes * 60
    log.info(
        f"Waiting {delay_minutes} min before processing. "
        f"Delete the file to cancel: {path}"
    )

    check_interval = 10
    elapsed = 0
    while elapsed < delay_seconds:
        if not path.exists():
            log.info(f"File deleted during delay window — upload cancelled: {path}")
            return False
        time.sleep(check_interval)
        elapsed += check_interval
    return True


# ============================================================
# REMUX
# ============================================================
def video_codec(path: Path) -> str | None:
    """Return the video stream's codec name, or None if it can't be read."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or None
    except Exception as e:
        log.warning(f"Could not probe codec for {path.name}: {e}")
        return None


def remux_to_mp4(mkv_path: Path) -> Path | None:
    out_path = mkv_path.with_suffix(".mp4")
    if out_path.exists():
        log.info(f"MP4 already exists, skipping remux: {out_path}")
        return out_path

    codec = video_codec(mkv_path)
    cmd = [FFMPEG, "-y", "-i", str(mkv_path), "-c", "copy"]
    if codec == "hevc":
        # QuickTime needs hvc1 rather than hev1 to render HEVC video
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-movflags", "+faststart", str(out_path)]

    log.info(f"Remuxing {mkv_path.name} -> {out_path.name} (codec: {codec})")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"ffmpeg failed: {result.stderr[-500:]}")
        return None
    log.info(f"Remux complete: {out_path}")
    return out_path


# ============================================================
# TRANSCRIPTION
# ============================================================
def transcript_prefix(recorded_at: datetime, course: str, semester: str) -> Path:
    """Destination path prefix (no extension) for this recording's transcripts."""
    folder = TRANSCRIPT_ROOT / sanitize_for_path(semester) / sanitize_for_path(course)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{recorded_at.strftime('%Y-%m-%d %H%M')} {sanitize_for_path(course)}"
    return folder / stem


def transcribe_to_prefix(mp4_path: Path, out_prefix: Path) -> Path | None:
    """
    Produce .srt, .txt and .vtt at the given prefix.
    Returns the .srt path, or None on failure.
    """
    if not TRANSCRIBE_ENABLED:
        return None

    srt_path = out_prefix.with_suffix(".srt")

    if srt_path.exists():
        log.info(f"Transcript already exists, skipping: {srt_path}")
        return srt_path

    if not WHISPER_MODEL.exists():
        log.error(f"Whisper model not found: {WHISPER_MODEL}")
        return None

    wav_path = mp4_path.with_suffix(".16k.wav")

    try:
        log.info(f"Extracting audio for transcription: {mp4_path.name}")
        result = subprocess.run(
            [FFMPEG, "-y", "-i", str(mp4_path),
             "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
             str(wav_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error(f"Audio extraction failed: {result.stderr[-500:]}")
            return None

        log.info(f"Transcribing with whisper.cpp ({WHISPER_MODEL.name})...")
        start = time.monotonic()
        result = subprocess.run(
            [WHISPER,
             "-m", str(WHISPER_MODEL),
             "-f", str(wav_path),
             "-t", str(WHISPER_THREADS),
             "-osrt", "-otxt", "-ovtt",
             "-of", str(out_prefix)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error(f"Whisper failed: {result.stderr[-500:]}")
            return None

        elapsed = time.monotonic() - start
        log.info(f"Transcription complete in {int(elapsed)}s: {srt_path}")
        return srt_path if srt_path.exists() else None

    finally:
        wav_path.unlink(missing_ok=True)


def transcribe(mp4_path: Path, recorded_at: datetime,
               course: str, semester: str) -> Path | None:
    """Transcribe into the standard TRANSCRIPT_ROOT/<semester>/<course>/ location."""
    return transcribe_to_prefix(
        mp4_path, transcript_prefix(recorded_at, course, semester)
    )

# ============================================================
# YOUTUBE
# ============================================================
def get_youtube_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRETS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def find_or_create_playlist(youtube, title: str) -> str:
    """Return the playlist ID for `title`, creating the playlist if absent."""
    page_token = None
    while True:
        resp = youtube.playlists().list(
            part="snippet", mine=True, maxResults=50, pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            if item["snippet"]["title"] == title:
                log.info(f"Found playlist: {title} ({item['id']})")
                return item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info(f"Creating new playlist: {title}")
    resp = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title},
            "status": {"privacyStatus": "unlisted"},
        },
    ).execute()
    return resp["id"]


def add_to_playlist(youtube, playlist_id: str, video_id: str, attempts: int = 4):
    for i in range(attempts):
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={"snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }},
            ).execute()
            return
        except HttpError as e:
            if i == attempts - 1:
                raise
            wait = 2 ** i
            log.warning(f"Playlist insert failed ({e.resp.status}); retrying in {wait}s")
            time.sleep(wait)


def upload_thumbnail(youtube, video_id: str, thumbnail_path: str | None) -> bool:
    if not thumbnail_path:
        log.info("No thumbnail configured for this course; skipping.")
        return False
    if not Path(thumbnail_path).exists():
        log.warning(f"Thumbnail file not found: {thumbnail_path}")
        return False
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumbnail_path),
        ).execute()
        log.info(f"Thumbnail uploaded: {thumbnail_path}")
        return True
    except HttpError as e:
        log.error(f"Thumbnail upload failed: {e}")
        return False


def upload_captions(youtube, video_id: str, srt_path: Path | None) -> bool:
    if srt_path is None or not srt_path.exists():
        log.info("No transcript available; skipping caption upload.")
        return False
    try:
        youtube.captions().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "language": CAPTION_LANGUAGE,
                    "name": "",
                    "isDraft": False,
                }
            },
            media_body=MediaFileUpload(str(srt_path)),
        ).execute()
        log.info(f"Captions uploaded: {srt_path.name}")
        return True
    except HttpError as e:
        log.error(f"Caption upload failed: {e}")
        return False


def upload_video(youtube, file_path: Path, recorded_at: datetime,
                 course: str, semester: str,
                 title: str | None = None,
                 description: str | None = None) -> str:
    title = title or f"{course} Lecture {recorded_at.strftime('%m/%d/%y')}"
    description = description or DESCRIPTION_TEMPLATE.format(
        course=course,
        date_short=recorded_at.strftime("%m/%d/%y"),
        institution=INSTITUTION,
    )

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": CATEGORY_ID_EDUCATION,
            "tags": [course, semester, "lecture", INSTITUTION],
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
        },
        "recordingDetails": {
            "recordingDate": recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }

    log.info(f"Uploading: {title}")
    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status,recordingDetails",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info(f"Upload progress: {int(status.progress() * 100)}%")
    video_id = response["id"]
    log.info(f"Uploaded video ID: {video_id}")
    return video_id


def upload_and_categorize(file_path: Path) -> bool | str:
    """
    Full pipeline for one .mp4. Returns True if the video reached YouTube,
    SKIPPED if no course matched or the date is a no-class date (both
    deliberate, don't retry), or False on failure (worth retrying).
    Failures after the video upload are logged but still return True, so
    the retry loop never re-uploads.
    """
    global config_error_notified

    recorded_at = parse_recording_datetime(file_path)
    if recorded_at is None:
        log.error(f"Could not parse datetime from filename: {file_path.name}")
        return False

    # The watcher normally catches this before any work happens. This
    # second check covers a recording queued before its date was added to
    # no_class_dates. manual_upload.py calls upload_video directly and is
    # deliberately unaffected.
    if is_day_off(recorded_at):
        log.info(
            f"No class on {recorded_at.date().isoformat()} — holding "
            f"{file_path.name} instead of uploading."
        )
        if str(file_path) not in notified_paths:
            notified_paths.add(str(file_path))
            notify(f"Day off recording held: {file_path.name}",
                   title="Recording Held", priority=0)
        return SKIPPED

    course, semester, thumbnail = determine_course(recorded_at)
    if semester is None:
        log.error("No 'semester' key in courses.json; cannot proceed.")
        if not config_error_notified:
            config_error_notified = True
            notify("courses.json is missing its 'semester' key.",
                   title="Config Error", priority=1)
        return False
    if course is None:
        log.warning(f"No course matched for {recorded_at}; skipping upload.")
        if str(file_path) not in notified_paths:
            notified_paths.add(str(file_path))
            notify(
                f"No course scheduled at {recorded_at.strftime('%a %m/%d %I:%M %p')}. "
                f"Use manual_upload.py if you want it up.",
                title="Upload Skipped", priority=1,
            )
        return SKIPPED

    srt_path = transcribe(file_path, recorded_at, course, semester)

    try:
        youtube = get_youtube_service()
        video_id = upload_video(youtube, file_path, recorded_at, course, semester)
    except Exception as e:
        log.error(f"Video upload failed: {e}")
        if str(file_path) not in notified_paths:
            notified_paths.add(str(file_path))
            notify(f"{course} — upload failed, will keep retrying: {e}",
                   title="Upload Failed", priority=1)
        return False

    # --- Video is live. Nothing below may trigger a re-upload. ---
    try:
        playlist_title = f"{course} {semester}"
        playlist_id = find_or_create_playlist(youtube, playlist_title)
        add_to_playlist(youtube, playlist_id, video_id)
        log.info(f"Added to playlist: {playlist_title}")
    except Exception as e:
        log.error(f"Playlist step failed (video already uploaded): {e}")

    try:
        upload_thumbnail(youtube, video_id, thumbnail)
    except Exception as e:
        log.error(f"Thumbnail step failed (video already uploaded): {e}")

    try:
        upload_captions(youtube, video_id, srt_path)
    except Exception as e:
        log.error(f"Caption step failed (video already uploaded): {e}")

    # --- Refresh the public lecture index and deploy it ---
    try:
        from generate_lecture_index import generate_for_course
        generate_for_course(course, youtube=youtube, push=True)
    except Exception as e:
        log.error(f"Lecture index update failed (video already uploaded): {e}")

    log.info(
        f"Upload complete: {course} Lecture {recorded_at.strftime('%m/%d/%y')} "
        f"https://youtu.be/{video_id}"
    )
    notify(
        f"{course} Lecture {recorded_at.strftime('%m/%d/%y')}\n"
        f"https://youtu.be/{video_id}",
        title="Upload Complete", priority=-1,
    )

    # A success re-arms both alerts: this path is healthy, and
    # courses.json was readable enough to get here.
    notified_paths.discard(str(file_path))
    config_error_notified = False
    return True


# ============================================================
# WATCHER + RETRY LOOP
# ============================================================
processing_lock = threading.Lock()
processing_files: set[str] = set()


def try_upload(file_path: Path):
    if not is_wired_connection():
        log.info(f"Not on wired connection — queueing: {file_path}")
        queue_add(str(file_path))
        notify(f"On Wi-Fi — {file_path.name} queued, retrying every 5 min.",
               title="Upload Queued", priority=0)
        return
    result = upload_and_categorize(file_path)
    if result is not True and result != SKIPPED:
        queue_add(str(file_path))


def retry_loop():
    """Periodically drain the queue whenever a wired connection is available."""
    first = True
    while True:
        if not first:
            time.sleep(RETRY_INTERVAL_SECONDS)
        first = False

        q = queue_load()
        if not q:
            continue
        if not is_wired_connection():
            log.info(f"Queue has {len(q)} item(s) but not on wired connection.")
            continue

        log.info(f"Wired detected — processing {len(q)} queued upload(s)")
        succeeded = 0
        for path_str in list(q):
            path = Path(path_str)
            if not path.exists():
                log.warning(f"Queued file no longer exists, removing: {path_str}")
                queue_remove(path_str)
                continue
            result = upload_and_categorize(path)
            if result is True or result == SKIPPED:
                queue_remove(path_str)
                succeeded += 1

        remaining = len(queue_load())
        if remaining:
            log.warning(
                f"Queue drain finished: {succeeded} done, {remaining} still queued"
            )
        else:
            log.info(f"Queue drain finished: {succeeded} done, queue empty")


class RecordingHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        raw = event.src_path
        path = Path(raw.decode() if isinstance(raw, bytes) else raw)
        if path.suffix.lower() != ".mkv":
            return

        # macOS FSEvents can deliver the same creation event more than
        # once. Everything below this guard — including the day-off
        # notification — therefore runs at most once per path.
        with processing_lock:
            if str(path) in processing_files:
                log.info(f"Already processing, ignoring duplicate event: {path}")
                return
            processing_files.add(str(path))

        log.info(f"New recording detected: {path}")

        # Day-off check first: no cancel window, no remux, no transcription.
        # The .mkv stays exactly where OBS left it, for manual_upload.py.
        recorded_at = parse_recording_datetime(path)
        if recorded_at is None:
            log.warning(f"No timestamp in {path.name}; skipping day-off check.")
        elif is_day_off(recorded_at):
            log.info(
                f"No class on {recorded_at.date().isoformat()} — holding "
                f"{path.name}. Use manual_upload.py to publish it."
            )
            notify(f"Day off recording held: {path.name}",
                   title="Recording Held", priority=0)
            return

        wait_for_file_stable(path)

        if not wait_delay_with_cancel(path, UPLOAD_DELAY_MINUTES):
            return

        mp4_path = remux_to_mp4(path)
        if mp4_path is None:
            return
        try_upload(mp4_path)


# ============================================================
# DRY RUN
# ============================================================
def dry_run() -> int:
    """
    Report what the day-off check would do with the recordings already in
    WATCH_FOLDER. Uploads nothing, moves nothing, notifies nothing.
    """
    try:
        data = load_courses()
    except Exception as e:
        print(f"Could not read {COURSES_FILE}: {e}")
        return 1

    no_class = parse_no_class_dates(data, notify_problems=False)

    print(f"\ncourses.json : {COURSES_FILE}")
    print(f"semester     : {data.get('semester', '(missing)')}")
    if no_class:
        print(f"no-class days: {len(no_class)}")
        for d in sorted(no_class):
            print(f"               {d.isoformat()}  {d.strftime('%a')}")
    else:
        print("no-class days: none loaded")

    # Prefer the .mp4 when both exist, matching manual_upload.py.
    candidates: list[tuple[Path, datetime]] = []
    unparsed: list[Path] = []
    for path in sorted(WATCH_FOLDER.iterdir()):
        if path.suffix.lower() not in (".mkv", ".mp4"):
            continue
        if path.suffix.lower() == ".mkv" and path.with_suffix(".mp4").exists():
            continue
        recorded_at = parse_recording_datetime(path)
        if recorded_at is None:
            unparsed.append(path)
            continue
        candidates.append((path, recorded_at))

    print(f"\nRecordings in {WATCH_FOLDER}: {len(candidates)}\n")
    if not candidates:
        print("  (none with a parseable timestamp)")

    held = would_upload = unmatched = 0
    for path, recorded_at in candidates:
        if recorded_at.date() in no_class:
            verdict, detail = "HOLD  ", "no class this date"
            held += 1
        else:
            try:
                course, _, _ = determine_course(recorded_at)
            except Exception as e:
                course, detail = None, f"schedule lookup failed: {e}"
            if course:
                verdict, detail = "UPLOAD", course
                would_upload += 1
            else:
                verdict = "SKIP  "
                detail = "no course matches this time"
                unmatched += 1
        print(f"  {verdict}  {recorded_at.strftime('%a %Y-%m-%d %H:%M')}  "
              f"{path.name}  ({detail})")

    if unparsed:
        print(f"\n  No timestamp in the filename ({len(unparsed)}):")
        for path in unparsed:
            print(f"    {path.name}")

    print(f"\n{held} held, {would_upload} would upload, {unmatched} unmatched.")
    print("Nothing was uploaded, moved, or notified.\n")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Watch the OBS recordings folder and upload lectures."
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report which existing recordings would be held as days off "
             "and which would upload, then exit. Uploads and notifies nothing.",
    )
    args = ap.parse_args()

    if args.dry_run:
        return dry_run()

    log.info("Starting OBS watcher")
    TRANSCRIPT_ROOT.mkdir(parents=True, exist_ok=True)

    # Validate the schedule once at startup so a missing or malformed
    # no_class_dates key is reported now rather than on the first
    # recording. Deliberately not fatal: launchd has KeepAlive set, so
    # exiting here would produce a restart loop.
    try:
        no_class = parse_no_class_dates(load_courses())
        log.info(f"Loaded {len(no_class)} no-class date(s) from courses.json")
    except Exception as e:
        log.error(f"Could not read courses.json at startup: {e}")

    observer = Observer()
    observer.schedule(RecordingHandler(), str(WATCH_FOLDER), recursive=False)
    observer.start()

    threading.Thread(target=retry_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
