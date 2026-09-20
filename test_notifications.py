#!/usr/bin/env python3
"""
Verify Pushover notifications for the OBS watcher pipeline.

Imports obs_watcher and calls its notify() directly, so no recordings,
network changes, or YouTube uploads are involved. Nothing is uploaded
and no files are touched.

Usage:
    python3 test_notifications.py              # run all tests
    python3 test_notifications.py --list       # show test names, send nothing
    python3 test_notifications.py --only success failed
    python3 test_notifications.py --delay 5    # seconds between sends
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import obs_watcher as ow
except ImportError as e:
    sys.exit(
        f"Could not import obs_watcher from {SCRIPT_DIR}: {e}\n"
        f"If config.py is missing, copy config.example.py to config.py."
    )


# A stand-in timestamp and video id so messages look like the real thing
FAKE_TIME = datetime(2026, 8, 12, 9, 5, 0)
FAKE_VIDEO_ID = "dQw4w9WgXcQ"
FAKE_COURSE = "MAT 305A"
FAKE_FILENAME = "2026-08-12 09-05-00.mp4"


def t_success():
    """Upload Complete — priority -1, silent. Sent after a successful upload."""
    ow.notify(
        f"{FAKE_COURSE} Lecture {FAKE_TIME.strftime('%m/%d/%y')}\n"
        f"https://youtu.be/{FAKE_VIDEO_ID}",
        title="Upload Complete", priority=-1,
    )


def t_failed():
    """Upload Failed — priority 1, bypasses quiet hours."""
    ow.notify(
        f"{FAKE_COURSE} — upload failed: "
        f"('invalid_grant: Token has been expired or revoked.')",
        title="Upload Failed", priority=1,
    )


def t_skipped():
    """Upload Skipped — priority 1. No course matched the recording time."""
    ow.notify(
        f"No course scheduled at {FAKE_TIME.strftime('%a %m/%d %I:%M %p')}. "
        f"Use manual_upload.py if you want it up.",
        title="Upload Skipped", priority=1,
    )


def t_queued():
    """Upload Queued — priority 0. Not on a wired connection."""
    ow.notify(
        f"On Wi-Fi — {FAKE_FILENAME} queued, retrying every 5 min.",
        title="Upload Queued", priority=0,
    )


TESTS = {
    "success": t_success,
    "failed": t_failed,
    "skipped": t_skipped,
    "queued": t_queued,
}


def check_credentials() -> bool:
    print("Checking configuration...")
    print(f"  NOTIFY_ENABLED : {ow.NOTIFY_ENABLED}")
    print(f"  token in keychain : {'yes' if ow.PUSHOVER_TOKEN else 'NO'}")
    print(f"  user key in keychain : {'yes' if ow.PUSHOVER_USER else 'NO'}")

    if not ow.NOTIFY_ENABLED:
        print("\nNOTIFY_ENABLED is False — notify() will return without sending.")
        return False
    if not (ow.PUSHOVER_TOKEN and ow.PUSHOVER_USER):
        print("\nCredentials missing. Add them with:")
        print(f'  security add-generic-password -a "$USER" '
              f'-s {ow.PUSHOVER_TOKEN_SERVICE} -w <your app token>')
        print(f'  security add-generic-password -a "$USER" '
              f'-s {ow.PUSHOVER_USER_SERVICE} -w <your user key>')
        return False

    # Sanity check on shape without printing the secrets
    for label, value in (("token", ow.PUSHOVER_TOKEN), ("user", ow.PUSHOVER_USER)):
        if len(value) != 30:
            print(f"  ! {label} is {len(value)} chars; Pushover keys are normally 30. "
                  f"Check for a stray newline or truncation.")
    print("  Configuration looks OK.\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", choices=sorted(TESTS),
                    help="Run only the named tests")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="Seconds to pause between sends (default: 3)")
    ap.add_argument("--list", action="store_true",
                    help="List the tests and exit without sending")
    args = ap.parse_args()

    if args.list:
        for name, fn in TESTS.items():
            print(f"{name:<10} {fn.__doc__.strip()}")
        return

    if not check_credentials():
        sys.exit(1)

    names = args.only or list(TESTS)
    print(f"Sending {len(names)} notification(s). "
          f"Watch your phone.\n")

    for i, name in enumerate(names, 1):
        fn = TESTS[name]
        print(f"[{i}/{len(names)}] {name}: {fn.__doc__.strip()}")
        fn()
        if i < len(names):
            time.sleep(args.delay)

    print("\nAll sends attempted.")
    print("Anything that failed is logged as a warning in:")
    print(f"  {ow.LOG_FILE}")
    print("\nExpected on your phone, in order:")
    print("  Upload Complete  — arrives silently (priority -1)")
    print("  Upload Failed    — makes a sound, bypasses quiet hours (priority 1)")
    print("  Upload Skipped   — makes a sound, bypasses quiet hours (priority 1)")
    print("  Upload Queued    — normal alert (priority 0)")


if __name__ == "__main__":
    main()
