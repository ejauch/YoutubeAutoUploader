#!/usr/bin/env python3
"""
Manual uploader for recordings the watcher couldn't match to a scheduled class.

Scans WATCH_FOLDER for recordings with no transcript on disk, lets you pick
one, pick a course from courses.json, and give a chapter number. Titles them
"<COURSE> Extra Video for Chapter <N>", then runs the same transcribe ->
upload -> playlist -> thumbnail -> captions path the watcher uses.

Usage:
    python3 manual_upload.py             # interactive
    python3 manual_upload.py --all       # list candidates and exit
    python3 manual_upload.py --dry-run   # stop at the summary, write nothing
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import obs_watcher as ow
    from config import INSTITUTION, EXTRA_DESCRIPTION_TEMPLATE
except ImportError as e:
    sys.exit(
        f"Could not import from {SCRIPT_DIR}: {e}\n"
        f"If config.py is missing, copy config.example.py to config.py."
    )


# ============================================================
# CANDIDATE DISCOVERY
# ============================================================
def has_transcript(recorded_at: datetime) -> bool:
    """
    True if any transcript anywhere under TRANSCRIPT_ROOT carries this
    recording's timestamp. Transcript stems start with 'YYYY-MM-DD HHMM'.
    """
    stamp = recorded_at.strftime("%Y-%m-%d %H%M")
    return any(ow.TRANSCRIPT_ROOT.rglob(f"{stamp}*.srt"))


def find_candidates() -> list[tuple[Path, datetime]]:
    """Recordings in WATCH_FOLDER with a parseable name and no transcript."""
    found = []
    for path in sorted(ow.WATCH_FOLDER.iterdir()):
        if path.suffix.lower() not in (".mp4", ".mkv"):
            continue
        recorded_at = ow.parse_recording_datetime(path)
        if recorded_at is None:
            continue
        # Prefer the .mp4 when both exist
        if path.suffix.lower() == ".mkv" and path.with_suffix(".mp4").exists():
            continue
        if has_transcript(recorded_at):
            continue
        found.append((path, recorded_at))
    return found


# ============================================================
# PROMPTS
# ============================================================
def choose(prompt: str, options: list[str]) -> int | None:
    """Print a numbered menu and return the chosen index, or None to abort."""
    for i, label in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        raw = input(f"{prompt} (1-{len(options)}, or 'q' to quit): ").strip()
        if raw.lower() in ("q", "quit", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("  Not a valid choice.")


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


# ============================================================
# MAIN FLOW
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="List candidates and exit without uploading")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render but write nothing")
    args = ap.parse_args()

    candidates = find_candidates()
    if not candidates:
        print("No recordings without transcripts found in "
              f"{ow.WATCH_FOLDER}.")
        return

    queued = set(ow.queue_load())

    print(f"\nRecordings with no transcript ({len(candidates)}):\n")
    labels = []
    for path, recorded_at in candidates:
        flag = "  [in upload queue]" if str(path) in queued else ""
        size_mb = path.stat().st_size / (1024 * 1024)
        labels.append(
            f"{recorded_at.strftime('%a %m/%d/%y %I:%M %p')}  "
            f"{path.name}  ({size_mb:.0f} MB){flag}"
        )

    if args.all:
        for label in labels:
            print(f"  - {label}")
        return

    idx = choose("Which recording?", labels)
    if idx is None:
        return
    video_path, recorded_at = candidates[idx]

    # --- Course selection ---
    data = ow.load_courses()
    semester = data.get("semester")
    courses = data.get("courses", [])
    if not semester:
        sys.exit("No 'semester' key in courses.json.")
    if not courses:
        sys.exit("No courses defined in courses.json.")

    print(f"\nCourses for {semester}:\n")
    cidx = choose("Which course?", [c["name"] for c in courses])
    if cidx is None:
        return
    course_entry = courses[cidx]
    course = course_entry["name"]
    thumbnail = course_entry.get("thumbnail")

    # --- Chapter (optional if you're supplying your own title) ---
    chapter = input("\nChapter number (blank to write your own title): ").strip()

    if chapter:
        default_suffix = f"Extra Video for Chapter {chapter}"
    else:
        default_suffix = "Extra Video"

    raw = input(f"Title: {course} [{default_suffix}]: ").strip()
    suffix = raw or default_suffix

    # --- Derived metadata ---
    title = f"{course} {suffix}"
    if chapter and suffix == default_suffix:
        description = EXTRA_DESCRIPTION_TEMPLATE.format(
            course=course, chapter=chapter, institution=INSTITUTION
        )
    else:
        description = (f"This is {suffix.lower()} for {course} "
                       f"at {INSTITUTION}")
    playlist_title = f"{course} {semester}"

    print("\n" + "=" * 60)
    print(f"  File        : {video_path.name}")
    print(f"  Recorded    : {recorded_at.strftime('%A %m/%d/%y at %I:%M %p')}")
    print(f"  Title       : {title}")
    print(f"  Description : {description}")
    print(f"  Playlist    : {playlist_title}")
    print(f"  Thumbnail   : {thumbnail or '(none configured)'}")
    print(f"  Privacy     : {ow.PRIVACY_STATUS}")
    print("=" * 60 + "\n")

    if args.dry_run:
        print("Dry run — stopping before remux, transcription, and upload.")
        return

    if not confirm("Proceed?"):
        print("Aborted.")
        return

    # --- Remux if we only have the .mkv ---
    if video_path.suffix.lower() == ".mkv":
        print("Remuxing to .mp4 ...")
        remuxed = ow.remux_to_mp4(video_path)
        if remuxed is None:
            sys.exit("Remux failed; see the log for details.")
        video_path = remuxed

    # --- Transcribe into the course folder, with an 'Extra Ch#' stem ---
    folder = (ow.TRANSCRIPT_ROOT / ow.sanitize_for_path(semester)
              / ow.sanitize_for_path(course))
    folder.mkdir(parents=True, exist_ok=True)
    stem = (f"{recorded_at.strftime('%Y-%m-%d %H%M')} "
            f"{ow.sanitize_for_path(course)} {ow.sanitize_for_path(suffix)}")
    out_prefix = folder / stem

    print("Transcribing (about a minute for a 50-minute recording) ...")
    srt_path = ow.transcribe_to_prefix(video_path, out_prefix)
    if srt_path is None:
        print("Transcription failed — continuing without captions.")

    # --- Upload ---
    print("Uploading ...")
    try:
        youtube = ow.get_youtube_service()
        video_id = ow.upload_video(
            youtube, video_path, recorded_at, course, semester,
            title=title, description=description,
        )
    except Exception as e:
        ow.log.error(f"Manual upload failed: {e}")
        sys.exit(f"Upload failed: {e}")

    # --- Video is live; nothing below should abort the run ---
    for label, fn in (
        ("playlist", lambda: ow.add_to_playlist(
            youtube, ow.find_or_create_playlist(youtube, playlist_title), video_id)),
        ("thumbnail", lambda: ow.upload_thumbnail(youtube, video_id, thumbnail)),
        ("captions", lambda: ow.upload_captions(youtube, video_id, srt_path)),
    ):
        try:
            fn()
        except Exception as e:
            print(f"  ! {label} step failed (video is already up): {e}")

    # --- Clear it from the retry queue if the watcher had parked it ---
    if str(video_path) in queued:
        ow.queue_remove(str(video_path))
        print("  Removed from the watcher's upload queue.")

    url = f"https://youtu.be/{video_id}"
    print(f"\nDone: {url}")
    ow.notify(f"{title}\n{url}", title="Manual Upload Complete", priority=-1)


if __name__ == "__main__":
    main()
