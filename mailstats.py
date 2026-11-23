"""Merge extracted emails into a permanent master file and compute TAT / query insights.

Shared by the laptop scripts (analyze.py) and the Easypanel tracker (tracker.py).
The data folder is TRACKER_DATA (default ./data next to the caller).

Usage:  python analyze.py
Reads:  data/raw/*.jsonl          (pages written by the extractors)
        data/emails_master.jsonl  (permanent record - only ever appended to)
Writes: data/emails_master.jsonl, data/threads.csv, data/insights/insights_<date>.json
"""
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("TRACKER_DATA", Path.cwd() / "data"))
RAW = DATA / "raw"
MASTER = DATA / "emails_master.jsonl"
INSIGHTS = DATA / "insights"

OUR_ADDRESS = "research.smallcase@niveshaay.com"
IST = timezone(timedelta(hours=5, minutes=30))
WORK_START, WORK_END = 9.5, 18.5   # 09:30-18:30 IST
WORK_DAYS = {0, 1, 2, 3, 4, 5}     # Mon-Sat

# Auto-generated mail that is not a client query and must not start a TAT clock
NOISE_SUBJECT = re.compile(
    r"how was your recent smallcase interaction|^automatic reply|out of office|undeliverable|delivery status",
    re.I)
NOISE_SUMMARY = re.compile(
    r"a support ticket has been created|we.ve received your request|got 7 seconds\?", re.I)
NOISE_SENDERS = {"notifications@smallcase.com"}
AUTOMATED_SENDER = re.compile(
    r"no.?reply|donotreply|do-not-reply|mailer-daemon|postmaster|notification|newsletter|"
    r"verify|message-service|alerts?@|updates?@|info@|marketing@|support@(?!smallcase)", re.I)
# Personal-mail domains: a sender here is almost always a retail client
CONSUMER_DOMAINS = re.compile(
    r"@(gmail|googlemail|yahoo|ymail|rocketmail|hotmail|outlook|live|msn|icloud|me|rediffmail|"
    r"rediff|aol|proton|protonmail|zoho)\.", re.I)
# smallcase only needs us when it explicitly hands the query over
SMALLCASE_LOOP_IN = re.compile(
    r"hi,? niveshaay|hello,? niveshaay|team niveshaay|niveshaay team|dear niveshaay|"
    r"smallcase manager|please provide an update|@research\.smallcase|looped? in|"
    r"please have a look|kindly assist", re.I)
# A short client message that only thanks, confirms or agrees needs no further email
CLOSURE_WORDS = re.compile(
    r"\b(ok(ay)?|sure|noted|thanks?|thank ?you|thankyou|thx|got it|received|works|agree[ds]?|"
    r"confirm(ed)?|go ahead|proceed)\b", re.I)
GREETING = re.compile(r"^\W*(hi|hello|dear|respected)\b[^\n,]{0,30}[,\n]?", re.I)
SIGNOFF = re.compile(r"\n\s*(regards|best regards|thanks & regards|warm regards|sincerely|thanking you)\b.*", re.I | re.S)


def is_closure(text):
    body = SIGNOFF.sub("", GREETING.sub("", text or "")).strip()
    return len(body) <= 200 and "?" not in body and bool(CLOSURE_WORDS.search(body))

TICKET = re.compile(r"TKT-\d+")
PREFIX = re.compile(r"^\s*((re|fw|fwd|aw)\s*:\s*|\[external\]\s*)+", re.I)

# First match wins; checked against subject + summary of the thread's opening query
CATEGORIES = [
    ("Refund / cancellation", r"refund|cancel|unsubscrib|stop (the )?subscription|money back|revers|by mistake|mistakenly|accidental"),
    ("Renewal pricing / discount", r"discount|coupon|offer|promo|renew|price|pricing|\bfees?\b|expensive|costly"),
    ("Payment / mandate", r"mandate|auto.?debit|autopay|payment|deducted|charged|invoice|receipt|gst"),
    ("KYC / broker / account", r"\bkyc\b|broker|demat|\bpan\b|login|account|e-?sign|aadhaar"),
    ("Unable to invest / platform issue", r"unable|not able|error|issue|problem|not working|can.?t|cannot|failed|rebalanc\w* (not|fail)"),
    ("Stock / portfolio advice", r"\bhold\b|exit|\bsell\b|\bbuy\b|stock|share|ipo|rights issue|rebalanc|portfolio|weight|allocation|performance|return|loss|volatil|fall|drop|underperform"),
    ("Subscription / plan info", r"subscri|plan|smallcase|theme|basket|minimum|investment amount"),
]


def load_jsonl(path):
    rows, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return rows, bad


def merge_master():
    master = {}
    if MASTER.exists():
        for m in load_jsonl(MASTER)[0]:
            master[m["mid"]] = m
    before = len(master)
    bad_total = 0
    for f in sorted(RAW.glob("*.jsonl")):
        rows, bad = load_jsonl(f)
        bad_total += bad
        for m in rows:
            if m.get("mid"):
                master.setdefault(m["mid"], m)   # never overwrite what is already stored
    rows = sorted(master.values(), key=lambda m: m.get("received") or "")
    with MASTER.open("w", encoding="utf-8") as fh:
        for m in rows:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    return rows, len(master) - before, bad_total


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def business_hours(start, end):
    """Hours between two datetimes counting only IST working hours."""
    if end <= start:
        return 0.0
    total, cur = 0.0, start.astimezone(IST)
    end = end.astimezone(IST)
    while cur < end:
        day_start = cur.replace(hour=int(WORK_START), minute=int(WORK_START % 1 * 60), second=0, microsecond=0)
        day_end = cur.replace(hour=int(WORK_END), minute=int(WORK_END % 1 * 60), second=0, microsecond=0)
        if cur.weekday() in WORK_DAYS:
            lo, hi = max(cur, day_start), min(end, day_end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return total


def addr(s):
    a = (s or "").strip().lower()
    # Replies to marketing campaigns arrive as name.gmail.com@reply.marketing.niveshaay.com
    if a.endswith("@reply.marketing.niveshaay.com"):
        parts = a.split("@")[0].rsplit(".", 2)
        if len(parts) == 3:
            a = f"{parts[0]}@{parts[1]}.{parts[2]}"
    return a


def own_words(summary):
    """The part of a message preview written by the sender, before any quoted reply."""
    s = summary or ""
    s = re.split(r"\n\s*On .{0,80}(wrote|<)|\n-{3,}|\n_{5,}|\nFrom:", s)[0]
    return s.strip()


def short(text, n=400):
    """One-line preview of at most n characters."""
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def is_ours(a):
    return addr(a) == OUR_ADDRESS


def is_external_party(a):
    a = addr(a)
    # Clients often mistype our address (niveshay.com, niveshaav.com, ...)
    return (a and not a.startswith("research.smallcase@") and not a.endswith("@niveshaay.com")
            and not a.endswith("smallcase.com"))


def client_of(m):
    parties = [m.get("from")] + list(m.get("to") or [])
    for a in parties:
        if is_external_party(a):
            return addr(a)
    return ""


def norm_subject(s):
    s = PREFIX.sub("", s or "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def categorise(text):
    for name, pattern in CATEGORIES:
        if re.search(pattern, text, re.I):
            return name
    return "Other"


def classify_message(m):
    if is_ours(m.get("from")):
        return "outbound"
    if addr(m.get("from")) in NOISE_SENDERS:
        return "noise"
    if NOISE_SUBJECT.search(m.get("subject") or "") or NOISE_SUMMARY.search(m.get("summary") or ""):
        return "noise"
    sender = addr(m.get("from"))
    if sender.endswith("@niveshaay.com"):
        return "internal"
    if sender.endswith("smallcase.com"):
        return "inbound" if SMALLCASE_LOOP_IN.search(m.get("summary") or "") else "fyi"
    if AUTOMATED_SENDER.search(sender):
        return "noise"
    return "inbound"


def build_threads(rows):
    # Ticket id links smallcase loop-ins with the client's own replies; otherwise subject + client
    threads = defaultdict(list)
    ticket_key = {}
    for m in rows:
        m["kind"] = classify_message(m)
        m["dt"] = parse_dt(m["received"])
        subj = norm_subject(m.get("subject"))
        client = client_of(m)
        key = (subj, client)
        tkt = TICKET.search(m.get("summary") or "")
        if tkt:
            ticket_key.setdefault(tkt.group(), key)
            key = ticket_key[tkt.group()]
        m["thread"] = key
        threads[key].append(m)
    return threads


def analyse(threads, now):
    out = []
    for key, msgs in threads.items():
        msgs.sort(key=lambda m: m["dt"])
        inbound = [m for m in msgs if m["kind"] == "inbound"]
        if not inbound:
            continue  # nothing a client (or smallcase on a client's behalf) asked
        first_q = inbound[0]
        text = f"{first_q.get('subject') or ''} {first_q.get('summary') or ''}"

        # Each run of inbound messages until our next reply is one "wait"
        waits, open_since, chasers, replied_once, closed = [], None, 0, False, False
        for m in msgs:
            if m["kind"] == "inbound":
                if replied_once and is_closure(own_words(m.get("summary"))):
                    closed = open_since is None
                    continue
                closed = False
                if open_since is not None:
                    chasers += 1
                else:
                    open_since = m["dt"]
            elif m["kind"] == "outbound":
                replied_once = True
                if open_since is not None:
                    waits.append((open_since, m["dt"]))
                    open_since = None

        sender = addr(first_q.get("from"))
        via_smallcase = sender.endswith("smallcase.com")
        is_client = via_smallcase or replied_once or bool(CONSUMER_DOMAINS.search(key[1] or sender))

        first_resp = waits[0] if waits and waits[0][0] == first_q["dt"] else None
        last_q = inbound[-1]
        ours = [m for m in msgs if m["kind"] == "outbound"]
        out.append({
            "first_preview": short(own_words(first_q.get("summary"))),
            "last_preview": short(own_words(last_q.get("summary"))) if last_q is not first_q else "",
            "last_client_at": last_q["dt"],
            "last_reply_preview": short(own_words(ours[-1].get("summary"))) if ours else "",
            "last_reply_at": ours[-1]["dt"] if ours else None,
            "client": key[1] or sender,
            "is_client": is_client,
            "subject": first_q.get("subject") or "",
            "category": categorise(text) if is_client else "Non-client (vendor / spam / system)",
            "via_smallcase": via_smallcase,
            "ticket": next((t.group() for m in msgs if (t := TICKET.search(m.get("summary") or ""))), ""),
            "opened": first_q["dt"],
            "last_activity": msgs[-1]["dt"],
            "messages": len(msgs),
            "inbound_msgs": len(inbound),
            "our_replies": sum(m["kind"] == "outbound" for m in msgs),
            "chasers": chasers,
            "first_response_h": (first_resp[1] - first_resp[0]).total_seconds() / 3600 if first_resp else None,
            "first_response_bh": business_hours(*first_resp) if first_resp else None,
            "avg_response_h": sum((b - a).total_seconds() for a, b in waits) / 3600 / len(waits) if waits else None,
            "status": "Awaiting our reply" if open_since else "Closed by client" if closed else "Replied",
            "waiting_h": (now - open_since).total_seconds() / 3600 if open_since else None,
        })
    return out


def pct(values, p):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p
    f = int(k)
    return v[f] + (v[min(f + 1, len(v) - 1)] - v[f]) * (k - f)


def summarise(rows, all_threads, now):
    kinds = Counter(m["kind"] for m in rows)
    threads_out = [t for t in all_threads if t["is_client"]]
    waiting = [t["waiting_h"] for t in threads_out if t["waiting_h"] is not None]
    ageing = {
        "under_1_day": sum(h < 24 for h in waiting),
        "1_to_3_days": sum(24 <= h < 72 for h in waiting),
        "3_to_7_days": sum(72 <= h < 168 for h in waiting),
        "over_7_days": sum(h >= 168 for h in waiting),
    }
    fr = [t["first_response_h"] for t in threads_out if t["first_response_h"] is not None]
    frb = [t["first_response_bh"] for t in threads_out if t["first_response_bh"] is not None]

    by_cat = defaultdict(list)
    for t in threads_out:
        by_cat[t["category"]].append(t)
    cats = []
    for c, ts in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        r = [t["first_response_bh"] for t in ts if t["first_response_bh"] is not None]
        cats.append({
            "category": c, "threads": len(ts),
            "share": round(len(ts) / len(threads_out), 3),
            "median_first_response_bh": round(pct(r, .5), 2) if r else None,
            "awaiting_reply": sum(t["status"] == "Awaiting our reply" for t in ts),
            "with_chasers": sum(t["chasers"] > 0 for t in ts),
        })

    weekly = Counter(t["opened"].astimezone(IST).strftime("%G-W%V") for t in threads_out)
    sla = lambda h: round(sum(x <= h for x in frb) / len(frb), 3) if frb else None

    return {
        "generated_at": now.isoformat(),
        "period": {"from": rows[0]["received"], "to": rows[-1]["received"]} if rows else None,
        "emails": {"total": len(rows), **kinds},
        "threads": {
            "client_threads": len(threads_out),
            "non_client_threads_excluded": len(all_threads) - len(threads_out),
            "awaiting_reply_ageing": ageing,
            "via_smallcase": sum(t["via_smallcase"] for t in threads_out),
            "direct_from_client": sum(not t["via_smallcase"] for t in threads_out),
            "awaiting_reply": sum(t["status"] == "Awaiting our reply" for t in threads_out),
            "with_chasers": sum(t["chasers"] > 0 for t in threads_out),
        },
        "first_response": {
            "answered_threads": len(fr),
            "median_h": round(pct(fr, .5), 2) if fr else None,
            "p90_h": round(pct(fr, .9), 2) if fr else None,
            "median_business_h": round(pct(frb, .5), 2) if frb else None,
            "p90_business_h": round(pct(frb, .9), 2) if frb else None,
            "within_1_bh": sla(1), "within_4_bh": sla(4), "within_9_bh_1day": sla(9),
        },
        "categories": cats,
        "weekly_new_threads": dict(sorted(weekly.items())),
        "top_clients_by_threads": Counter(t["client"] for t in threads_out).most_common(15),
        "oldest_awaiting_reply": [
            {"client": t["client"], "subject": t["subject"], "category": t["category"],
             "waiting_h": round(t["waiting_h"], 1), "chasers": t["chasers"]}
            for t in sorted(threads_out, key=lambda t: -(t["waiting_h"] or 0)) if t["waiting_h"]
        ][:25],
    }


def main(verbose=True):
    rows, added, bad = merge_master()
    now = datetime.now(timezone.utc)
    threads = build_threads(rows)
    threads_out = analyse(threads, now)

    with (DATA / "threads.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(threads_out[0].keys()) if threads_out else ["client"])
        w.writeheader()
        for t in sorted(threads_out, key=lambda t: t["opened"]):
            w.writerow({k: (v.astimezone(IST).strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime)
                            else round(v, 2) if isinstance(v, float) else v) for k, v in t.items()})

    summary = summarise(rows, threads_out, now)
    summary["merge"] = {"new_emails_added": added, "unparseable_lines": bad}
    INSIGHTS.mkdir(parents=True, exist_ok=True)
    snap = INSIGHTS / f"insights_{now.astimezone(IST):%Y-%m-%d}.json"
    snap.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if verbose:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return summary


if __name__ == "__main__":
    main()
