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

Machine-specific settings live in config.py — copy config.example.py to
config.py and edit it before first run.

Uploads are only attempted on a wired connection. On Wi-Fi the file is
queued and retried every RETRY_INTERVAL_SECONDS.
"""

import re
import sys
import time
import json
import logging
import subprocess
import threading
from typing import Any
from datetime import datetime, timedelta
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

# Sentinel: matched no scheduled course. Treated like success by the
# queue so an unmatched recording isn't retried forever.
SKIPPED = "skipped"

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
    SKIPPED if no course matched (deliberate, don't retry), or False on
    failure (worth retrying). Failures after the video upload are logged
    but still return True, so the retry loop never re-uploads.
    """
    global config_error_notified

    recorded_at = parse_recording_datetime(file_path)
    if recorded_at is None:
        log.error(f"Could not parse datetime from filename: {file_path.name}")
        return False

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

    log.info(f"Upload complete: {course} Lecture {recorded_at.strftime('%m/%d/%y')}")
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

        # macOS FSEvents can deliver the same creation event more than once.
        with processing_lock:
            if str(path) in processing_files:
                log.info(f"Already processing, ignoring duplicate event: {path}")
                return
            processing_files.add(str(path))

        log.info(f"New recording detected: {path}")
        wait_for_file_stable(path)

        if not wait_delay_with_cancel(path, UPLOAD_DELAY_MINUTES):
            return

        mp4_path = remux_to_mp4(path)
        if mp4_path is None:
            return
        try_upload(mp4_path)


def main():
    log.info("Starting OBS watcher")
    TRANSCRIPT_ROOT.mkdir(parents=True, exist_ok=True)

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


if __name__ == "__main__":
    main()
