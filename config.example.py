"""
Local configuration for YoutubeAutoUploader.

Copy this file to config.py and edit it for your machine:

    cp config.example.py config.py

config.py is gitignored. This file is tracked, so keep it free of
anything machine-specific.
"""

from pathlib import Path

# ============================================================
# PATHS
# ============================================================
# Resolves to the directory this file lives in. Leave as-is.
SCRIPT_DIR = Path(__file__).resolve().parent

# Where OBS writes finished recordings.
WATCH_FOLDER = Path.home() / "Movies"

# Transcripts are filed as <root>/<semester>/<course>/.
TRANSCRIPT_ROOT = Path.home() / "Transcripts"

# Created automatically at startup. launchd's StandardOutPath and
# StandardErrorPath should point here too — but launchd will NOT create
# the directory for you, so it must exist before the agent is loaded.
LOG_DIR = SCRIPT_DIR / "logs"

# ============================================================
# BINARIES
# ============================================================
# Homebrew on Apple Silicon uses /opt/homebrew/bin; Intel macOS uses
# /usr/local/bin; most Linux distributions use /usr/bin.
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
WHISPER = "/opt/homebrew/bin/whisper-cli"

# ============================================================
# TRANSCRIPTION
# ============================================================
# Models: https://huggingface.co/ggerganov/whisper.cpp
# medium.en is a good default — roughly a minute for a 47-minute lecture
# on an M4 Max. small.en is about twice as fast but mangles domain
# vocabulary badly enough to make the captions untrustworthy.
# benchmark_whisper.py looks for other models in this same directory.
WHISPER_MODEL = SCRIPT_DIR / "models" / "ggml-medium.en.bin"
WHISPER_THREADS = 8

# False skips transcription and caption upload entirely.
TRANSCRIBE_ENABLED = True
CAPTION_LANGUAGE = "en"

# ============================================================
# VIDEO METADATA
# ============================================================
# "public", "unlisted", or "private".
PRIVACY_STATUS = "unlisted"

# Used in video descriptions, video tags, and the lecture-page footer.
INSTITUTION = "Your Institution"

# Scheduled lectures. Fields: {course}, {date_short}, {institution}
DESCRIPTION_TEMPLATE = (
    "This is the class lecture for {course} on {date_short} "
    "at {institution}."
)

# manual_upload.py, for one-off videos outside the class schedule.
# Fields: {course}, {chapter}, {institution}
EXTRA_DESCRIPTION_TEMPLATE = (
    "This is an extra video for {course}, Chapter {chapter}, "
    "at {institution}"
)

# ============================================================
# TIMING
# ============================================================
# A recording starting this many minutes before a scheduled class still
# counts as that class.
PRE_CLASS_GRACE_MINUTES = 15

# Cancel window: delete the .mkv during this period to abort the upload.
# 0 disables the wait and processes immediately.
UPLOAD_DELAY_MINUTES = 1

# How often to retry queued uploads while off a wired connection.
RETRY_INTERVAL_SECONDS = 300

# ============================================================
# NOTIFICATIONS (Pushover)
# ============================================================
NOTIFY_ENABLED = True
PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Credentials are read from the macOS keychain, never stored here.
# Create the entries with:
#   security add-generic-password -a "$USER" -s <service-name> -w
PUSHOVER_TOKEN_SERVICE = "pushover_obs_uploader_token"
PUSHOVER_USER_SERVICE = "pushover_user"

# ============================================================
# LECTURE INDEX SITE (generate_lecture_index.py)
# ============================================================
# Local checkout of the static site. Pages land at
#   SITE_REPO / SITE_ASSETS / SITE_SUBDIR / <semester> / <slug> / index.html
# and serve from https://<domain>/<SITE_SUBDIR>/<semester>/<slug>/
SITE_REPO = Path.home() / "Documents" / "GitHub" / "your-site"

# Cloudflare Pages serves assets.directory as the site root, so everything
# is written inside it and the served URL drops the prefix. Keep this in
# sync with "assets": {"directory": ...} in wrangler.jsonc. Use "" if your
# host serves the repository root directly.
SITE_ASSETS = "public"

# URL path segment under the site root: /Lectures/FA26/MAT215A/
SITE_SUBDIR = "Lectures"

# Cloudflare Pages URLs are case-sensitive. False keeps the case of the
# source (SITE_SUBDIR as typed, the semester from courses.json, and the
# course name with spaces stripped). True lowercases the whole path.
LOWERCASE_PATHS = False

# True means playlist position 0 is the most recent lecture; numbering
# counts down so lecture 1 stays the first meeting of the semester.
# Per-course override: "newest_first" in courses.json.
NEWEST_FIRST = True
