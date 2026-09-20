#!/usr/bin/env python3
"""
generate_lecture_index.py

Builds a static lecture-index page per course from its YouTube playlist, for
hosting on Cloudflare Pages and embedding into Canvas via a single iframe.

Why this exists: YouTube's embedded player no longer reliably renders the
playlist queue panel, so the list of lectures is rebuilt here as plain HTML
links driving a single player iframe.

Reads courses.json in the same shape obs_watcher.py uses:

    {"semester": "FA26", "courses": [{"name": "MAT 215A", ...}, ...]}

No new required keys. Playlists are resolved by title the same way the
uploader does ("<name> <semester>"), and the output path is derived from the
course name. Three optional keys are honored if present:

    "title":        human-readable course title for the page heading
    "page_slug":    override the derived URL slug
    "newest_first": override NEWEST_FIRST for this course

Usage:
    python3 generate_lecture_index.py --all
    python3 generate_lecture_index.py --course "MAT 215A"
    python3 generate_lecture_index.py --all --push
    python3 generate_lecture_index.py --all --config --dry-run

From obs_watcher.py, after a successful upload:
    from generate_lecture_index import generate_for_course
    generate_for_course(course, push=True)
"""

from __future__ import annotations

import argparse
import html
import logging
import subprocess
import sys
import json
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Raise rather than sys.exit: obs_watcher imports this module inside a
# try/except Exception after a video is already live, and SystemExit
# would slip past that handler and kill the watcher.
try:
    import obs_watcher as ow
    from config import (
        SITE_REPO,
        SITE_ASSETS,
        SITE_SUBDIR,
        LOWERCASE_PATHS,
        NEWEST_FIRST,
        INSTITUTION,
    )
except ImportError as e:
    raise ImportError(
        f"Could not import dependencies from {SCRIPT_DIR}: {e}. "
        f"If config.py is missing, copy config.example.py to config.py."
    ) from e

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
# SITE_REPO, SITE_ASSETS, SITE_SUBDIR, LOWERCASE_PATHS, NEWEST_FIRST and
# INSTITUTION are imported from config.py above. Everything else
# (courses.json path, OAuth token, scopes) comes from obs_watcher, so
# there is exactly one source of truth.

# Archived per-semester course files: courses/courses-SP26.json etc.
COURSES_DIR = SCRIPT_DIR / "courses"

log = logging.getLogger("lecture_index")



# --------------------------------------------------------------------------
# Courses files
# --------------------------------------------------------------------------


def resolve_courses_path(arg: str) -> Path:
    """
    Accept a bare semester code ('SP26'), a filename
    ('courses-SP26.json'), or any explicit path.
    """
    p = Path(arg)
    if p.is_absolute() or p.exists():
        return p
    if not p.suffix:
        p = Path(f"courses-{arg.upper()}.json")
    for base in (COURSES_DIR, SCRIPT_DIR):
        if (base / p).exists():
            return base / p
    sys.exit(f"No courses file found for '{arg}' (looked in {COURSES_DIR})")


def load_courses(path: Path | None = None) -> dict:
    """Current semester via obs_watcher, or an archived file when given."""
    if path is None:
        return ow.load_courses()
    with open(path) as f:
        return json.load(f)


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------


def playlist_title_for(course_name, semester):
    """Match the uploader's naming exactly, or we'd look up the wrong list."""
    return f"{course_name} {semester}"


def find_playlist_id(youtube, title):
    """
    Return the playlist ID for `title`, or None.

    Deliberately not ow.find_or_create_playlist: generating an index page
    should never create an empty playlist as a side effect.
    """
    page_token = None
    while True:
        resp = youtube.playlists().list(
            part="snippet", mine=True, maxResults=50, pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            if item["snippet"]["title"] == title:
                return item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return None


def fetch_playlist_items(playlist_id, youtube):
    """
    Return [{title, video_id, position, published_at, private}] in playlist
    order, paginating through the whole list.

    Deleted and private entries are kept but flagged, so a pulled video still
    consumes its lecture number instead of shifting everything below it.
    """
    items, page_token = [], None

    while True:
        resp = youtube.playlistItems().list(
            part="snippet,contentDetails,status",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        for entry in resp.get("items", []):
            snippet = entry.get("snippet", {})
            resource = snippet.get("resourceId", {})
            privacy = entry.get("status", {}).get("privacyStatus", "")
            title = snippet.get("title", "")

            # snippet.publishedAt is when the video was ADDED to the playlist.
            # contentDetails.videoPublishedAt is when the video itself went up,
            # which is the date that matches the class meeting.
            published = (
                entry.get("contentDetails", {}).get("videoPublishedAt")
                or snippet.get("publishedAt", "")
            )

            items.append({
                "title": title,
                "video_id": resource.get("videoId", ""),
                "position": snippet.get("position", 0),
                "published_at": published,
                "private": (
                    privacy in ("private", "privacyStatusUnspecified")
                    and title in ("Private video", "Deleted video")
                ),
            })

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    items.sort(key=lambda i: i["position"])
    return items


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{heading_esc} \u2014 Lecture Recordings</title>
<style>
  :root {{
    --navy: #003767;
    --sky: #79bde8;
    --ink: #16202b;
    --muted: #5a6875;
    --rule: #d7dee5;
    --paper: #ffffff;
    --wash: #f2f6f9;
  }}

  * {{ box-sizing: border-box; }}

  body {{
    margin: 0;
    padding: 1.5rem 1.25rem 2.5rem;
    background: var(--paper);
    color: var(--ink);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}

  .wrap {{ max-width: 1080px; margin: 0 auto; }}

  h1 {{
    font-family: Charter, "Bitstream Charter", Georgia, serif;
    font-size: 1.6rem;
    font-weight: 600;
    line-height: 1.2;
    margin: 0 0 .2rem;
    color: var(--navy);
  }}

  .subhead {{
    font-family: Charter, "Bitstream Charter", Georgia, serif;
    font-size: 1rem;
    color: var(--muted);
    margin: 0 0 1.4rem;
  }}

  .player {{
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
    border-radius: 3px;
    overflow: hidden;
  }}
  .player iframe {{
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    border: 0;
  }}

  .now-playing {{
    font-size: .95rem;
    color: var(--muted);
    margin: .7rem 0 1.6rem;
    min-height: 1.4em;
  }}

  h2 {{
    font-family: Charter, "Bitstream Charter", Georgia, serif;
    font-size: 1.15rem;
    font-weight: 600;
    margin: 0 0 .6rem;
    padding-bottom: .5rem;
    border-bottom: 2px solid var(--navy);
  }}

  ol.lectures {{ list-style: none; margin: 0; padding: 0; }}

  ol.lectures li {{ border-bottom: 1px solid var(--rule); }}

  ol.lectures a {{
    display: grid;
    grid-template-columns: 2.9rem 1fr auto;
    gap: .85rem;
    align-items: baseline;
    padding: .7rem .55rem;
    color: var(--ink);
    text-decoration: none;
  }}

  .lec-num {{
    font-variant-numeric: tabular-nums;
    font-size: .9rem;
    color: var(--muted);
    text-align: right;
  }}

  ol.lectures a:hover,
  ol.lectures a:focus-visible {{ background: var(--wash); }}
  ol.lectures a:focus-visible {{ outline: 2px solid var(--navy); outline-offset: -2px; }}

  ol.lectures a[aria-current="true"] {{
    background: var(--wash);
    box-shadow: inset 4px 0 0 var(--sky);
  }}
  ol.lectures a[aria-current="true"] .lec-title {{ font-weight: 600; }}

  .lec-date {{
    font-size: .85rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }}

  li.unavailable a {{ color: var(--muted); pointer-events: none; }}
  li.unavailable .lec-title::after {{ content: " (unavailable)"; font-size: .85rem; }}

  footer {{ margin-top: 2rem; font-size: .85rem; color: var(--muted); }}

  .empty {{ padding: 1.5rem .55rem; color: var(--muted); }}

  @media (max-width: 640px) {{
    ol.lectures a {{ grid-template-columns: 2.1rem 1fr; }}
    .lec-date {{ grid-column: 2; font-size: .8rem; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <h1>{heading_esc}</h1>
  <p class="subhead">{subhead_esc}</p>

  <div class="player">
    <iframe id="player"
            title="Lecture video player"
            src="https://www.youtube.com/embed/{first_video}?rel=0"
            referrerpolicy="strict-origin-when-cross-origin"
            allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
            allowfullscreen></iframe>
  </div>
  <p class="now-playing" id="nowPlaying" role="status" aria-live="polite"></p>

  <h2>All lectures</h2>
  {list_html}

  <footer>
    <p>{count} recording{plural} &middot; updated {updated}</p>
    <p>{institution}. Recordings are unlisted and intended for enrolled students.</p>
  </footer>

</div>

<script>
(function () {{
  var player = document.getElementById('player');
  var status = document.getElementById('nowPlaying');
  var links = document.querySelectorAll('ol.lectures a[data-video]');

  function select(link) {{
    var id = link.getAttribute('data-video');
    player.src = 'https://www.youtube.com/embed/' + id + '?rel=0&autoplay=1';
    links.forEach(function (a) {{ a.removeAttribute('aria-current'); }});
    link.setAttribute('aria-current', 'true');
    status.textContent = 'Now playing: ' + link.querySelector('.lec-title').textContent;
  }}

  links.forEach(function (link) {{
    link.addEventListener('click', function (event) {{
      // Let modified clicks open YouTube in a new tab as normal.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      select(link);
    }});
  }});

  if (links.length) {{
    links[0].setAttribute('aria-current', 'true');
    status.textContent = 'Now playing: ' + links[0].querySelector('.lec-title').textContent;
  }}
}})();
</script>
</body>
</html>
"""


def _format_date(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d")
    except ValueError:
        return ""


def render_page(name, title, semester, items, newest_first=NEWEST_FIRST):
    """
    Render one course page.

    The heading is the course title when courses.json supplies one, with the
    course code carried in the subhead so it stays visible. Without a title
    the code becomes the heading and the subhead falls back to a label, so
    the code is never printed twice.

    Items are displayed in playlist order. The lecture number is always
    chronological: lecture 1 is the first meeting of the semester regardless
    of which end of the list it sits on.
    """
    # Resolved before the row loop, which rebinds `title` per lecture.
    heading = title or name
    subhead = (f"{name} \u00b7 {semester}" if title
               else f"Lecture recordings \u00b7 {semester}")

    watchable = [i for i in items if not i["private"] and i["video_id"]]
    total = len(items)

    if watchable:
        rows = []
        for index, item in enumerate(items):
            number = total - index if newest_first else index + 1
            unavailable = item["private"] or not item["video_id"]
            cls = ' class="unavailable"' if unavailable else ""
            lec_title = html.escape(item["title"])
            date = html.escape(_format_date(item["published_at"]))
            num = f'<span class="lec-num">{number}</span>'

            if unavailable:
                rows.append(
                    f'<li{cls}><a>{num}<span class="lec-title">{lec_title}</span>'
                    f'<span class="lec-date">{date}</span></a></li>'
                )
            else:
                vid = html.escape(item["video_id"])
                rows.append(
                    f'<li><a href="https://www.youtube.com/watch?v={vid}" '
                    f'data-video="{vid}">{num}'
                    f'<span class="lec-title">{lec_title}</span>'
                    f'<span class="lec-date">{date}</span></a></li>'
                )

        attrs = f' reversed start="{total}"' if newest_first else ""
        list_html = (
            f'<ol class="lectures"{attrs}>\n    ' + "\n    ".join(rows) + "\n  </ol>"
        )
        first_video = html.escape(watchable[0]["video_id"])
    else:
        list_html = (
            '<p class="empty">No recordings posted yet. '
            "Check back after the first class meeting.</p>"
        )
        first_video = ""

    return PAGE_TEMPLATE.format(
        heading_esc=html.escape(heading),
        subhead_esc=html.escape(subhead),
        list_html=list_html,
        first_video=first_video,
        count=len(watchable),
        plural="" if len(watchable) == 1 else "s",
        updated=datetime.now().strftime("%b %-d, %Y at %-I:%M %p"),
        institution=html.escape(INSTITUTION),
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def slug_for(course):
    """URL slug from the course name: 'MAT 215A' -> 'MAT215A'."""
    if course.get("page_slug"):
        return course["page_slug"]
    slug = "".join(ch for ch in course["name"] if ch.isalnum())
    return slug.lower() if LOWERCASE_PATHS else slug


def url_subdir():
    """URL path segment, without the assets-directory prefix."""
    return SITE_SUBDIR.lower() if LOWERCASE_PATHS else SITE_SUBDIR


def assets_root():
    """On-disk directory Wrangler publishes as the site root."""
    return SITE_REPO / SITE_ASSETS


def output_path_for(course, semester):
    sem = semester.lower() if LOWERCASE_PATHS else semester
    return assets_root() / url_subdir() / sem / slug_for(course) / "index.html"


def generate_for_course(course_name, youtube=None, push=False, dry_run=False, courses_path=None):
    """
    Regenerate one course's index page. Returns the written Path, or None if
    the course or its playlist could not be found.
    """
    data = load_courses(courses_path)
    semester = data.get("semester")
    if not semester:
        log.error("No 'semester' key in %s", courses_path or "courses.json")
        return None

    course = next(
        (c for c in data.get("courses", []) if c["name"] == course_name), None
    )
    if course is None:
        log.warning("%s not found in courses.json", course_name)
        return None

    youtube = youtube or ow.get_youtube_service()

    title = playlist_title_for(course["name"], semester)
    playlist_id = find_playlist_id(youtube, title)
    if not playlist_id:
        log.info("No playlist named %r yet; skipping %s", title, course_name)
        return None

    items = fetch_playlist_items(playlist_id, youtube)
    page = render_page(
        course["name"],
        course.get("title"),
        semester,
        items,
        newest_first=course.get("newest_first", NEWEST_FIRST),
    )
    dest = output_path_for(course, semester)

    if dry_run:
        log.info("[dry run] would write %d bytes to %s", len(page), dest)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    log.info("Wrote %s (%d lectures)", dest, len(items))

    if push:
        git_publish([dest], message=f"Update {course_name} lecture index")

    return dest


def write_site_config(dry_run=False):
    """
    robots.txt and _headers at the site root. Blanket-disallow, so the file
    does not advertise which paths exist.
    """
    # Scoped to the lectures directory, not the whole site: ecjauch.com is a
    # professional page that should stay indexable. Each generated page also
    # carries a noindex meta tag, so this is belt-and-braces, not the only
    # line of defense.
    # Written into the assets directory, but the paths inside them are URL
    # paths, which do not include the assets-directory prefix.
    sub = url_subdir()
    files = {
        "robots.txt": f"User-agent: *\nDisallow: /{sub}/\n",
        "_headers": f"/{sub}/*\n  X-Robots-Tag: noindex, nofollow\n",
    }
    written = []
    for filename, body in files.items():
        path = assets_root() / filename
        if dry_run:
            log.info("[dry run] would write %s", path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written.append(path)
        log.info("Wrote %s", path)
    return written


def git_publish(paths, message):
    """Stage, commit, push. No-ops cleanly when nothing changed."""
    rel = [str(Path(p).relative_to(SITE_REPO)) for p in paths]
    try:
        subprocess.run(["git", "add", *rel], cwd=SITE_REPO, check=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=SITE_REPO, capture_output=True, text=True,
        )
        if result.returncode != 0:
            if "nothing to commit" in (result.stdout + result.stderr):
                log.info("No changes to publish")
                return False
            log.error("git commit failed: %s", result.stderr.strip())
            return False
        subprocess.run(["git", "push"], cwd=SITE_REPO, check=True)
        log.info("Pushed to Cloudflare Pages")
        return True
    except subprocess.CalledProcessError as exc:
        log.error("git failed: %s", exc)
        return False
    except FileNotFoundError:
        log.error("git not found on PATH")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--course", help='Course name, e.g. "MAT 215A"')
    group.add_argument("--all", action="store_true", help="Every course in courses.json")
    parser.add_argument("--push", action="store_true", help="git commit and push")
    parser.add_argument("--dry-run", action="store_true", help="Render but write nothing")
    parser.add_argument("--config", action="store_true",
                        help="Also write robots.txt and _headers")
    parser.add_argument("--semester", metavar="CODE",
                        help="Archived semester: a code (SP26), a filename, "
                             "or a path. Defaults to the current courses.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    courses_path = resolve_courses_path(args.semester) if args.semester else None
    data = load_courses(courses_path)
    names = ([c["name"] for c in data.get("courses", [])]
             if args.all else [args.course])

    youtube = ow.get_youtube_service()
    written = []

    for name in names:
        try:
            path = generate_for_course(name, youtube=youtube, dry_run=args.dry_run,
                                       courses_path=courses_path)
            if path:
                written.append(path)
        except Exception:
            log.exception("Failed to generate %s", name)

    if args.config:
        written.extend(write_site_config(dry_run=args.dry_run))

    if args.push and written and not args.dry_run:
        git_publish(written, message="Update lecture indexes")

    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
