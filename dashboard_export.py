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


TAGS = A.DATA / "enrich" / "tags.jsonl"
# Spellings Claude used for the same stock or basket, mapped to one name (lower-case keys)
ALIASES = {
    "gc hotels": "Grand Continental Hotels", "grand continent hotels": "Grand Continental Hotels",
    "hle glascoat": "HLE Glasscoat", "sansera": "Sansera Engineering", "acutaas": "Acutaas Chemicals",
    "esds": "ESDS Software Solution", "genesys": "Genesys International", "krn": "KRN Heat Exchanger",
    "ipo basket fundamental": "IPO Basket", "ai data centre value chains theme": "AI & Data Centre",
    "ai & data center theme": "AI & Data Centre", "mid and small cap focused portfolio fundamental": "Mid & Small Cap Focused",
    "green energy theme": "Green Energy", "niveshaay techstack": "TechStack", "tech stack": "TechStack",
}


def _canon(names_by_thread):
    """One spelling per stock/basket: case and spacing differences are merged, the most used spelling wins."""
    spellings = {}
    for names in names_by_thread:
        for n in names:
            k = " ".join(n.lower().replace(".", " ").replace("ltd", " ").replace("limited", " ").split())
            spellings.setdefault(k, Counter())[n.strip()] += 1
    return {k: c.most_common(1)[0][0] for k, c in spellings.items()}


def add_tags(rows):
    """Join Claude's tags (theme, question, stocks, baskets, faq, sentiment) onto each conversation."""
    tags = {}
    if TAGS.exists():
        for line in TAGS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                for f in ("stocks", "baskets"):
                    t[f] = [ALIASES.get(n.strip().lower(), n.strip()) for n in t.get(f) or []]
                tags[t["id"]] = t
    stock_names = _canon(t.get("stocks") or [] for t in tags.values())
    basket_names = _canon(t.get("baskets") or [] for t in tags.values())
    key = lambda n: " ".join(n.lower().replace(".", " ").replace("ltd", " ").replace("limited", " ").split())
    for r in rows:
        t = tags.get(r["id"])
        if not t:
            r.update(theme="Not tagged yet", question="", stocks=[], baskets=[], faq="", sentiment="")
            continue
        r.update(
            theme=t.get("theme") or "Other",
            question=t.get("question") or "",
            stocks=sorted({stock_names[key(s)] for s in t.get("stocks") or [] if s.strip()}),
            baskets=sorted({basket_names[key(b)] for b in t.get("baskets") or [] if b.strip()}),
            faq=t.get("faq") or "",
            sentiment=t.get("sentiment") or "",
        )


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

    add_tags(out)

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
