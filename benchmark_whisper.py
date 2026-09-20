#!/usr/bin/env python3
"""
Benchmark whisper.cpp transcription on existing lecture recordings.

Usage:
    python3 benchmark_whisper.py /path/to/video1.mp4 [/path/to/video2.mp4 ...]
    python3 benchmark_whisper.py --model small.en /path/to/video.mp4

Extracts audio, runs whisper.cpp, and reports wall-clock time and
the realtime factor (how many minutes of processing per minute of audio).
Outputs land next to the source video.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from config import FFMPEG, FFPROBE, WHISPER, WHISPER_MODEL, WHISPER_THREADS
except ImportError as e:
    sys.exit(
        f"Could not import config from {SCRIPT_DIR}: {e}\n"
        f"Copy config.example.py to config.py and edit it for this machine."
    )

# Benchmark other models sitting beside the one the pipeline uses
MODEL_DIR = WHISPER_MODEL.parent
THREADS = WHISPER_THREADS


def media_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def extract_audio(video: Path, wav_path: Path) -> float:
    """Extract 16 kHz mono WAV. Returns seconds spent."""
    start = time.monotonic()
    subprocess.run(
        [FFMPEG, "-y", "-i", str(video),
         "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
         str(wav_path)],
        capture_output=True, check=True,
    )
    return time.monotonic() - start


def transcribe(wav_path: Path, model: str, out_prefix: Path) -> float:
    """Run whisper.cpp producing srt + txt + vtt. Returns seconds spent."""
    model_path = MODEL_DIR / f"ggml-{model}.bin"
    if not model_path.exists():
        sys.exit(f"Model not found: {model_path}")

    start = time.monotonic()
    subprocess.run(
        [WHISPER,
         "-m", str(model_path),
         "-f", str(wav_path),
         "-t", str(THREADS),
         "-osrt", "-otxt", "-ovtt",
         "-of", str(out_prefix)],
        check=True,
    )
    return time.monotonic() - start


def fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+", type=Path)
    ap.add_argument("--model", default="medium.en",
                    help="Model name without the ggml- prefix (default: medium.en)")
    ap.add_argument("--keep-wav", action="store_true",
                    help="Don't delete the extracted WAV afterward")
    args = ap.parse_args()

    results = []

    for video in args.videos:
        if not video.exists():
            print(f"!! Skipping, not found: {video}")
            continue

        print(f"\n=== {video.name} ===")
        duration = media_duration_seconds(video)
        print(f"Duration: {fmt(duration)}")

        wav_path = video.with_suffix(".16k.wav")
        out_prefix = video.with_suffix("")

        print("Extracting audio...")
        extract_secs = extract_audio(video, wav_path)
        print(f"  audio extract: {fmt(extract_secs)}")

        print(f"Transcribing with {args.model}...")
        transcribe_secs = transcribe(wav_path, args.model, out_prefix)

        if not args.keep_wav:
            wav_path.unlink(missing_ok=True)

        total = extract_secs + transcribe_secs
        rtf = transcribe_secs / duration if duration else 0

        print(f"  transcription: {fmt(transcribe_secs)}")
        print(f"  TOTAL:         {fmt(total)}")
        print(f"  realtime factor: {rtf:.2f}x "
              f"({rtf * 60:.1f} sec of processing per minute of audio)")

        results.append({
            "file": video.name,
            "duration_sec": round(duration),
            "extract_sec": round(extract_secs),
            "transcribe_sec": round(transcribe_secs),
            "total_sec": round(total),
            "realtime_factor": round(rtf, 3),
        })

    if results:
        print("\n=== SUMMARY ===")
        print(f"{'file':<40} {'audio':>8} {'total':>8} {'RTF':>6}")
        for r in results:
            print(f"{r['file'][:39]:<40} {fmt(r['duration_sec']):>8} "
                  f"{fmt(r['total_sec']):>8} {r['realtime_factor']:>6.2f}")

        avg_rtf = sum(r["realtime_factor"] for r in results) / len(results)
        print(f"\nAverage realtime factor: {avg_rtf:.2f}x")
        print(f"Projected time for a 50-min lecture: {fmt(avg_rtf * 50 * 60)}")

        out_json = Path.cwd() / f"benchmark_{args.model}.json"
        out_json.write_text(json.dumps(results, indent=2))
        print(f"\nSaved: {out_json}")


if __name__ == "__main__":
    main()
