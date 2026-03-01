"""iCourse Subscriber — main orchestration.

Runs a single check: login → detect new lectures → download → transcribe
→ summarize → email. Designed to be triggered by GitHub Actions cron.
"""

import os
import time
import traceback

from src import config
from src.database import Database
from src.emailer import Emailer
from src.icourse import ICourseClient
from src.summarizer import Summarizer
from src.transcriber import Transcriber
from src.webvpn import WebVPNSession


def process_lecture(
    client: ICourseClient,
    db: Database,
    transcriber: Transcriber,
    summarizer: Summarizer,
    emailer: Emailer | None,
    course_id: str,
    course_title: str,
    lecture: dict,
):
    """Download, transcribe, summarize, and email a single lecture."""
    sub_id = str(lecture["sub_id"])
    sub_title = lecture.get("sub_title", sub_id)
    date = lecture.get("date", "")

    print(f"\n  -- Processing: {sub_title} ({date})")

    # 1) Download video
    video_url = client.get_video_url(course_id, sub_id)
    if not video_url:
        print(f"    No video URL for {sub_id}, skipping.")
        return

    video_dir = os.path.join(config.VIDEO_DIR, course_id)
    video_path = os.path.join(video_dir, f"{sub_id}.mp4")
    os.makedirs(video_dir, exist_ok=True)

    if not os.path.exists(video_path):
        print(f"    Downloading video...")
        client.download_video(video_url, video_path)

    # 2) Transcribe via ffmpeg pipe + SenseVoice
    print(f"    Transcribing...")
    transcript = transcriber.transcribe_video(video_path)
    db.update_transcript(sub_id, transcript)

    # 3) Delete video to save disk
    if os.path.exists(video_path):
        os.remove(video_path)
        print(f"    Video deleted.")

    # 4) Summarize
    if not transcript.strip():
        print(f"    Empty transcript, skipping summary.")
        db.mark_processed(sub_id)
        return

    print(f"    Generating summary...")
    summary = summarizer.summarize(course_title, transcript)
    db.update_summary(sub_id, summary)

    # 5) Email
    if emailer:
        print(f"    Sending email...")
        emailer.send(course_title, sub_title, date, summary)
        db.mark_emailed(sub_id)

    db.mark_processed(sub_id)
    print(f"    Done: {sub_title}")


def login_with_retry(max_attempts: int = 5) -> WebVPNSession:
    """Login to WebVPN + iCourse CAS with retry (new session each attempt)."""
    for attempt in range(max_attempts):
        try:
            vpn = WebVPNSession()
            print(f"\n[Login] WebVPN (attempt {attempt + 1}/{max_attempts})...")
            vpn.login()
            print("[Login] iCourse CAS...")
            vpn.authenticate_icourse()
            return vpn
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"  Failed: {type(e).__name__}, retrying...")
                time.sleep(3)
            else:
                raise


def run():
    """Single execution of the full pipeline."""
    print("=" * 60)
    print("iCourse Subscriber — starting run")
    print("=" * 60)

    if not config.COURSE_IDS:
        print("No COURSE_IDS configured. Set the COURSE_IDS env var.")
        return

    db = Database()
    transcriber = Transcriber()
    summarizer = Summarizer()
    emailer = Emailer() if config.SMTP_EMAIL and config.SMTP_PASSWORD else None

    vpn = login_with_retry()
    client = ICourseClient(vpn)

    for course_id in config.COURSE_IDS:
        try:
            print(f"\n{'─' * 50}")
            print(f"[Course] {course_id}")

            detail = client.get_course_detail(course_id)
            course_title = detail["title"]
            teacher = detail["teacher"]
            lectures = detail["lectures"]
            playback_count = sum(1 for l in lectures if l.get("has_playback"))
            print(f"  Title: {course_title} (Teacher: {teacher})")
            print(f"  Total lectures: {len(lectures)} ({playback_count} with playback)")

            db.upsert_course(course_id, course_title, teacher)

            # Find new lectures with playback + previously failed (unprocessed) ones
            known_processed = db.get_processed_sub_ids(course_id)
            new_lectures = [
                lec for lec in lectures
                if lec.get("has_playback")
                and str(lec["sub_id"]) not in known_processed
            ]
            # Also retry any previously inserted but unprocessed
            unprocessed = db.get_unprocessed_lectures(course_id)
            new_ids = {str(lec["sub_id"]) for lec in new_lectures}
            # Merge: new from API + retries from DB
            retry_only = [
                {"sub_id": u["sub_id"], "sub_title": u["sub_title"], "date": u["date"]}
                for u in unprocessed if u["sub_id"] not in new_ids
            ]
            new_lectures.extend(retry_only)

            print(f"  New/retry lectures: {len(new_lectures)}")

            if not new_lectures:
                print("  No new lectures, skipping.")
                continue

            for lecture in new_lectures:
                sub_id = str(lecture["sub_id"])
                db.insert_lecture(
                    sub_id, course_id,
                    lecture.get("sub_title", ""),
                    lecture.get("date", ""),
                )
                try:
                    process_lecture(
                        client, db, transcriber, summarizer, emailer,
                        course_id, course_title, lecture,
                    )
                except Exception:
                    print(f"    ERROR processing {sub_id}:")
                    traceback.print_exc()

        except Exception:
            print(f"  ERROR processing course {course_id}:")
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print("Run complete.")


if __name__ == "__main__":
    run()
