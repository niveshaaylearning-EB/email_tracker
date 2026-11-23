"""Export dashboard datasets from the analysed mailbox.

Shared by the laptop (export_dashboard.py) and the Easypanel tracker.
Usage:  python analyze.py && python export_dashboard.py
Writes: data/dashboard/threads.json  (one row per client conversation)
        data/dashboard/daily.json    (emails per day by type)
"""
import hashlib
import json
from collections import Counter

import mailstats as A

OUT = A.DATA / "dashboard"


def main():
    rows = [json.loads(l) for l in A.MASTER.read_text(encoding="utf-8").splitlines() if l.strip()]
    # Waiting times are measured to the latest email on record, not to today,
    # so the numbers don't grow while the data isn't refreshed
    now = max(A.parse_dt(m["received"]) for m in rows)
    threads = A.analyse(A.build_threads(rows), now)

    out = []
    for t in sorted(threads, key=lambda t: t["opened"]):
        if not t["is_client"]:
            continue
        opened = t["opened"].astimezone(A.IST)
        tid = hashlib.md5(f"{t['client']}|{A.norm_subject(t['subject'])}|{t['opened'].isoformat()}".encode()).hexdigest()[:10]
        rnd = lambda v, n=2: round(v, n) if v is not None else None
        out.append({
            "id": tid,
            "opened": opened.strftime("%Y-%m-%dT%H:%M"),
            "day": opened.strftime("%Y-%m-%d"),
            "hour": opened.hour,
            "weekday": opened.strftime("%a"),
            "category": t["category"],
            "channel": "via smallcase" if t["via_smallcase"] else "Direct",
            "status": t["status"],
            "first_response_bh": rnd(t["first_response_bh"]),
            "first_response_h": rnd(t["first_response_h"]),
            "chasers": t["chasers"],
            "our_replies": t["our_replies"],
            "messages": t["messages"],
            "waiting_days": rnd(t["waiting_h"] / 24, 1) if t["waiting_h"] is not None else None,
            "last_activity": t["last_activity"].astimezone(A.IST).strftime("%Y-%m-%d"),
            "client": t["client"],
            "subject": t["subject"].strip() or "(no subject)",
            "ticket": t["ticket"],
            "first_preview": t["first_preview"],
            "last_preview": t["last_preview"],
            "last_client_at": t["last_client_at"].astimezone(A.IST).strftime("%Y-%m-%d %H:%M"),
            "last_reply_preview": t["last_reply_preview"],
            "last_reply_at": t["last_reply_at"].astimezone(A.IST).strftime("%Y-%m-%d %H:%M") if t["last_reply_at"] else "",
        })

    daily = Counter()
    for m in rows:
        day = A.parse_dt(m["received"]).astimezone(A.IST).strftime("%Y-%m-%d")
        daily[(day, m["kind"])] += 1
    days = sorted({d for d, _ in daily})
    daily_rows = [{
        "day": d,
        "client_emails": daily[(d, "inbound")],
        "our_replies": daily[(d, "outbound")],
        "smallcase_fyi": daily[(d, "fyi")],
        "auto_or_spam": daily[(d, "noise")],
    } for d in days]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "threads.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    (OUT / "daily.json").write_text(json.dumps(daily_rows), encoding="utf-8")
    as_of = f"{now.astimezone(A.IST):%Y-%m-%d %H:%M} IST"
    print(f"threads: {len(out)}  days: {len(daily_rows)}  as of {as_of}")
    return {"threads": len(out), "days": len(daily_rows), "as_of": as_of}


if __name__ == "__main__":
    main()
