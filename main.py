"""Runs everything in one container:

  - the inbox sorter (Complaints / Queries), every INTERVAL_MINUTES, if ANTHROPIC_API_KEY is set
  - the tracker, which saves new mail and rebuilds the dashboard every REFRESH_HOURS (default 24)
  - the password-protected dashboard on PORT (default 8080)

Sign in once from the Easypanel console with:  python sorter.py --login
"""
import logging
import os
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent

import sorter  # noqa: E402
sorter.load_dotenv(HERE / ".env")

import tracker  # noqa: E402
import web  # noqa: E402

log = logging.getLogger("main")
LOCK = threading.Lock()   # one collection/rebuild at a time


def refresh():
    if not LOCK.acquire(blocking=False):
        return None
    try:
        return tracker.refresh_safely()
    finally:
        LOCK.release()


def tracker_loop():
    every = float(os.environ.get("REFRESH_HOURS", "24")) * 3600
    while True:
        wait = every
        try:
            refresh()
        except sorter.NeedsLogin as exc:
            log.error("Tracker: %s. Trying again in 30 min.", exc)
            wait = 1800
        except Exception:
            log.exception("Tracker run failed; trying again in 1 hour")
            wait = min(every, 3600)
        time.sleep(wait)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("SORTER_ENABLED", "1") != "0":
        threading.Thread(target=sorter.loop, name="sorter", daemon=True).start()
    else:
        log.info("Inbox sorter is off (no ANTHROPIC_API_KEY, or SORTER_ENABLED=0)")
    threading.Thread(target=tracker_loop, name="tracker", daemon=True).start()
    web.serve(LOCK, refresh, tracker.TRACKER_DATA)


if __name__ == "__main__":
    main()
