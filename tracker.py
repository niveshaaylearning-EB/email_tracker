"""Daily mailbox tracker: save every new email and rebuild the dashboard numbers.

Uses the same sign-in as the sorter (signin_cache.bin in DATA_DIR). Reads the
Inbox, Sent Items, Archive and "Confirmation pending" folders, appends new emails
to the permanent record (TRACKER_DATA/emails_master.jsonl, never overwritten),
then recomputes threads.json / daily.json for the dashboard.

Settings (environment):
  TRACKER_START          first run: collect mail received after this time (default 2026-07-01T00:00:00Z)
  REFRESH_HOURS          how often to collect (default 24)
  TRACKER_DATA           where the record and dashboard files live (default DATA_DIR/tracker)

Usage:
  python tracker.py      collect once and rebuild, then exit
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
os.environ.setdefault("TRACKER_DATA", str(DATA_DIR / "tracker"))
TRACKER_DATA = Path(os.environ["TRACKER_DATA"])

import dashboard_export  # noqa: E402  (both read TRACKER_DATA on import)
import enrich  # noqa: E402
import mailstats  # noqa: E402
from sorter import Mailbox  # noqa: E402

FOLDERS = ["Inbox", "Sent Items", "Archive", "Confirmation pending"]
WELL_KNOWN = {"Inbox": "inbox", "Sent Items": "sentitems", "Archive": "archive"}
OVERLAP = timedelta(days=1)     # re-read the last day each time; duplicates are dropped by message id
SELECT = "internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,bodyPreview,hasAttachments,isRead"

log = logging.getLogger("tracker")


def state_path():
    return TRACKER_DATA / "state.json"


def load_state():
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(state_path())


def find_folder(mb, name):
    """Folder id by name, looking at top-level folders and inside the Inbox. Never creates one."""
    if name in WELL_KNOWN:
        return WELL_KNOWN[name]
    for parent in (f"{mb.base}/mailFolders", f"{mb.base}/mailFolders/inbox/childFolders"):
        found = mb._call("GET", parent, params={"$filter": f"displayName eq '{name}'"}).get("value", [])
        if found:
            return found[0]["id"]
    return None


def to_row(msg, folder):
    """Same shape as the rows the laptop extractors wrote, so both merge into one record."""
    addr = lambda r: ((r or {}).get("emailAddress") or {}).get("address") or ""
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    recipients = [addr(r) for r in (msg.get("toRecipients") or []) + (msg.get("ccRecipients") or [])]
    return {
        "mid": msg.get("internetMessageId"),
        "folder": folder,
        "from": (sender.get("address") or "").lower() or None,
        "fromName": sender.get("name"),
        "to": [a.lower() for a in recipients if a],
        "received": msg.get("receivedDateTime"),
        "sent": msg.get("sentDateTime"),
        "subject": msg.get("subject") or "",
        "summary": msg.get("bodyPreview") or "",
        "attach": bool(msg.get("hasAttachments")),
        "isRead": bool(msg.get("isRead")),
    }


def collect(mb, since):
    rows = []
    for name in FOLDERS:
        fid = find_folder(mb, name)
        if not fid:
            log.info("Folder %r not found, skipped", name)
            continue
        url = f"{mb.base}/mailFolders/{fid}/messages"
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$select": SELECT,
            "$top": 100,
        }
        n = 0
        while url:
            page = mb._call("GET", url, params=params)
            for msg in page.get("value", []):
                if msg.get("internetMessageId"):
                    rows.append(to_row(msg, name))
                    n += 1
            url, params = page.get("@odata.nextLink"), None
        log.info("%s: %d emails since %s", name, n, since.date())
    return rows


def rebuild():
    """Merge everything in raw/ into the record and recompute the dashboard files."""
    summary = mailstats.main(verbose=False)
    out = dashboard_export.main()
    return summary.get("merge", {}), out


def run_once():
    (TRACKER_DATA / "raw").mkdir(parents=True, exist_ok=True)
    state = load_state()
    started = datetime.now(timezone.utc)
    if state.get("last_received"):
        since = datetime.fromisoformat(state["last_received"]) - OVERLAP
    else:
        since = datetime.fromisoformat(os.environ.get("TRACKER_START", "2026-07-01T00:00:00Z").replace("Z", "+00:00"))

    rows = collect(Mailbox(), since)
    if rows:
        path = TRACKER_DATA / "raw" / f"graph_{started:%Y%m%d_%H%M}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    merge, out = rebuild()
    try:
        if enrich.tag_new(TRACKER_DATA, state):
            out = dashboard_export.main()   # include the new tags
    except Exception:
        log.exception("Tagging failed; the dashboard is up to date except for the Weekly review tags")

    latest = max((r["received"] for r in rows), default=None)
    if latest:
        state["last_received"] = latest.replace("Z", "+00:00")
    state["last_run"] = started.isoformat()
    state["last_result"] = {"fetched": len(rows), **merge, **(out or {})}
    save_state(state)
    log.info("Tracker: fetched %d, new %s, dashboard %s", len(rows), merge.get("new_emails_added"), out)
    return state["last_result"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from sorter import load_dotenv
    load_dotenv(HERE / ".env")
    print(run_once())
