"""Copy each new Inbox email into a Complaints or Queries folder.

Runs as an always-on container (Easypanel or any Docker host) and checks the
Inbox every few minutes. It remembers the last email it handled, so after any
gap or restart it works through everything it missed, oldest first. The
original email always stays in the Inbox.

Settings come from environment variables (or a .env file next to this script):
  TENANT_ID, CLIENT_ID      the Entra app registration
  CLIENT_SECRET             only for admin-granted app access; leave empty to sign in
                            as the mailbox itself (run --login once)
  MAILBOX                   research.smallcase@niveshaay.com
  ANTHROPIC_API_KEY         Claude API key
  START_FROM                optional ISO time for the very first run
  DATA_DIR                  where progress, the sign-in and the sort log are kept (default ./data)
  INTERVAL_MINUTES          how often to check in --loop mode (default 5)

Usage:
  python sorter.py --login                    sign in once as the mailbox (shows a code)
  python sorter.py --loop                     keep running, check every INTERVAL_MINUTES
  python sorter.py                            one pass, then exit
  python sorter.py --dry-run --since 2026-11-04T00:00:00Z --max 10
                                              test: classify and print, copy nothing
"""
import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
HERE = Path(__file__).parent
REQUEST = json.loads((HERE / "base_request.json").read_text(encoding="utf-8"))
FOLDERS = {"COMPLAINT": "Complaints", "QUERY": "Queries"}
MAX_ATTEMPTS = 3          # after this many failures an email goes to Queries, flagged
MAX_BODY_CHARS = 4000     # only the new part of the email, trimmed, is sent to Claude
# Spending guards: past either limit, emails still get copied (to Queries, flagged) without calling Claude
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "100"))                  # Claude calls per day (UTC)
MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "15"))   # estimated Claude spend per month
# USD per million tokens for the model in base_request.json, used for the estimate
PRICE_IN = float(os.environ.get("PRICE_IN_PER_MTOK", "4"))
PRICE_OUT = float(os.environ.get("PRICE_OUT_PER_MTOK", "20"))
OVERLAP = timedelta(minutes=15)   # re-read a little before the checkpoint for late arrivals

# Mail that is not from a client is left alone (stays in the Inbox only)
NOISE_SENDER = re.compile(
    r"no.?reply|donotreply|mailer-daemon|postmaster|newsletter|notifications@smallcase\.com", re.I)
NOISE_SUBJECT = re.compile(
    r"how was your recent smallcase interaction|^automatic reply|out of office|undeliverable|delivery status", re.I)
NOISE_BODY = re.compile(r"a support ticket has been created|we.ve received your request", re.I)

log = logging.getLogger("inbox_sorter")


# ---------- progress store ----------

class Store:
    """Progress (state.json) and a monthly sort log, kept in DATA_DIR (a persistent volume)."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.dir = Path(os.environ.get("DATA_DIR", HERE / "data"))
        self.dir.mkdir(parents=True, exist_ok=True)

    def load_state(self):
        try:
            return json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_state(self, state):
        if self.dry_run:
            return
        # Write then rename, so a crash mid-write never leaves a broken file
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.dir / "state.json")

    def append_log(self, row):
        line = json.dumps(row, ensure_ascii=False) + "\n"
        if self.dry_run:
            print(line, end="")
            return
        path = self.dir / "log" / f"{row['received'][:7]}.jsonl"
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


# ---------- Microsoft Graph ----------

DELEGATED_SCOPES = ["Mail.ReadWrite", "User.Read"]


class NeedsLogin(RuntimeError):
    pass


class Mailbox:
    """The mailbox, reached either as the mailbox's own user (signed in once with --login)
    or, when CLIENT_SECRET is set, as an app an admin has granted access to."""

    def __init__(self):
        self.address = os.environ["MAILBOX"].lower()
        authority = f"https://login.microsoftonline.com/{os.environ['TENANT_ID']}"
        self.app_only = bool(os.environ.get("CLIENT_SECRET"))
        if self.app_only:
            self.app = msal.ConfidentialClientApplication(
                os.environ["CLIENT_ID"], authority=authority, client_credential=os.environ["CLIENT_SECRET"])
            self.base = f"{GRAPH}/users/{self.address}"
        else:
            self.cache_path = Path(os.environ.get("DATA_DIR", HERE / "data")) / "signin_cache.bin"
            self.cache = msal.SerializableTokenCache()
            if self.cache_path.exists():
                self.cache.deserialize(self.cache_path.read_text())
            self.app = msal.PublicClientApplication(
                os.environ["CLIENT_ID"], authority=authority, token_cache=self.cache)
            self.base = f"{GRAPH}/me"
        self.http = requests.Session()

    def _save_cache(self):
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize())
            try:
                self.cache_path.chmod(0o600)
            except OSError:
                pass

    def _token(self):
        if self.app_only:
            token = self.app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        else:
            accounts = self.app.get_accounts()
            token = self.app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0]) if accounts else None
            self._save_cache()
            if not token:
                raise NeedsLogin("Not signed in, or the sign-in expired: run `python sorter.py --login`")
        if "access_token" not in token:
            raise RuntimeError(f"Sign-in failed: {token.get('error')}: {token.get('error_description')}")
        return token["access_token"]

    def login(self):
        flow = self.app.initiate_device_flow(scopes=DELEGATED_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start sign-in: {flow.get('error_description', flow)}")
        print(flow["message"], flush=True)
        token = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in token:
            raise RuntimeError(f"Sign-in failed: {token.get('error')}: {token.get('error_description')}")
        self._save_cache()
        me = self._call("GET", f"{GRAPH}/me")
        signed_in = (me.get("mail") or me.get("userPrincipalName") or "").lower()
        print(f"Signed in as {signed_in}")
        if signed_in != self.address:
            print(f"WARNING: expected {self.address}. Run --login again and sign in with that account.")

    def _call(self, method, url, **kw):
        headers = {"Authorization": f"Bearer {self._token()}", **kw.pop("headers", {})}
        for attempt in range(4):
            r = self.http.request(method, url, headers=headers, timeout=60, **kw)
            if r.status_code in (429, 503, 504):
                time.sleep(int(r.headers.get("Retry-After", 5 * (attempt + 1))))
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()

    def folder_id(self, name):
        found = self._call("GET", f"{self.base}/mailFolders",
                           params={"$filter": f"displayName eq '{name}'"})["value"]
        if found:
            return found[0]["id"]
        return self._call("POST", f"{self.base}/mailFolders", json={"displayName": name})["id"]

    def inbox_since(self, since, limit):
        url = f"{self.base}/mailFolders/inbox/messages"
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$select": "id,internetMessageId,subject,from,receivedDateTime,bodyPreview,uniqueBody",
            "$top": 50,
        }
        out = []
        while url and len(out) < limit:
            page = self._call("GET", url, params=params, headers={"Prefer": 'outlook.body-content-type="text"'})
            out += page.get("value", [])
            url, params = page.get("@odata.nextLink"), None
        return out[:limit]

    def copy(self, message_id, folder_id, flag):
        copied = self._call("POST", f"{self.base}/messages/{message_id}/copy", json={"destinationId": folder_id})
        if flag:
            self._call("PATCH", f"{self.base}/messages/{copied['id']}", json={"flag": {"flagStatus": "flagged"}})
        return copied["id"]


# ---------- classification ----------

def sender_of(msg):
    ea = (msg.get("from") or {}).get("emailAddress") or {}
    return (ea.get("address") or "").lower(), ea.get("name") or ""


def skip_reason(msg, own_address):
    sender, _ = sender_of(msg)
    if sender == own_address:
        return "sent by us"
    if sender.endswith("@niveshaay.com"):
        return "internal"
    if NOISE_SENDER.search(sender):
        return "automated sender"
    if NOISE_SUBJECT.search(msg.get("subject") or ""):
        return "automated subject"
    if NOISE_BODY.search(msg.get("bodyPreview") or ""):
        return "ticket acknowledgement"
    return None


def classify(claude, msg):
    sender, name = sender_of(msg)
    body = ((msg.get("uniqueBody") or {}).get("content") or msg.get("bodyPreview") or "").strip()
    text = f"From: {name} <{sender}>\nSubject: {msg.get('subject') or ''}\n\n{body[:MAX_BODY_CHARS]}"
    resp = claude.messages.create(
        model=REQUEST["model"],
        max_tokens=REQUEST["max_tokens"],
        system=REQUEST["system"],
        output_config=REQUEST["output_config"],
        messages=[{"role": "user", "content": text}],
    )
    usage = {"in_tokens": resp.usage.input_tokens, "out_tokens": resp.usage.output_tokens}
    usage["cost_usd"] = round((usage["in_tokens"] * PRICE_IN + usage["out_tokens"] * PRICE_OUT) / 1e6, 5)
    if resp.stop_reason == "refusal":
        return {"label": "QUERY", "confidence": "low", "reason": "Claude declined to classify", **usage}
    answer = next(b.text for b in resp.content if b.type == "text")
    return {**json.loads(answer), **usage}


def over_budget(spend):
    """Why Claude should not be called right now, or None."""
    if spend["day_calls"] >= DAILY_LIMIT:
        return f"daily limit of {DAILY_LIMIT} reached"
    if spend["month_usd"] >= MONTHLY_BUDGET_USD:
        return f"monthly budget of ${MONTHLY_BUDGET_USD:g} reached"
    return None


# ---------- main loop ----------

def run(dry_run=False, since=None, max_messages=None):
    max_messages = max_messages or int(os.environ.get("MAX_PER_RUN", "40"))
    store = Store(dry_run)
    state = store.load_state()
    mailbox = Mailbox()
    claude = anthropic.Anthropic()

    if since:
        checkpoint = datetime.fromisoformat(since.replace("Z", "+00:00"))
    elif state.get("checkpoint"):
        checkpoint = datetime.fromisoformat(state["checkpoint"])
    else:
        start = os.environ.get("START_FROM")
        checkpoint = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else datetime.now(timezone.utc)

    done = state.get("done", {})          # message id -> received time, for the overlap window
    attempts = state.get("attempts", {})
    folders = state.get("folders") or {}
    if not dry_run:
        for label, name in FOLDERS.items():
            folders.setdefault(label, mailbox.folder_id(name))

    today = datetime.now(timezone.utc)
    spend = state.get("spend") or {}
    if spend.get("day") != today.strftime("%Y-%m-%d"):
        spend.update(day=today.strftime("%Y-%m-%d"), day_calls=0)
    if spend.get("month") != today.strftime("%Y-%m"):
        spend.update(month=today.strftime("%Y-%m"), month_usd=0.0, month_calls=0)

    counts = {"COMPLAINT": 0, "QUERY": 0, "skipped": 0, "failed": 0}
    # Ask for enough to get past emails already handled in the overlap window
    for msg in mailbox.inbox_since(checkpoint - OVERLAP, max_messages + len(done)):
        mid = msg["id"]
        if mid in done:
            continue
        received = datetime.fromisoformat(msg["receivedDateTime"].replace("Z", "+00:00"))
        sender, _ = sender_of(msg)
        row = {"received": msg["receivedDateTime"], "from": sender, "subject": msg.get("subject"),
               "internetMessageId": msg.get("internetMessageId")}

        reason = skip_reason(msg, mailbox.address)
        if reason:
            store.append_log({**row, "label": "SKIPPED", "reason": reason})
            counts["skipped"] += 1
        elif limit := over_budget(spend):
            log.warning("Not calling Claude: %s", limit)
            result = {"label": "QUERY", "confidence": "low", "reason": f"Not classified: {limit}"}
            if not dry_run:
                mailbox.copy(mid, folders["QUERY"], flag=True)
            store.append_log({**row, **result, "sorted_at": datetime.now(timezone.utc).isoformat()})
            counts["QUERY"] += 1
        else:
            try:
                result = classify(claude, msg)
                spend["day_calls"] += 1
                spend["month_calls"] = spend.get("month_calls", 0) + 1
                spend["month_usd"] = round(spend["month_usd"] + result.get("cost_usd", 0), 5)
            except Exception as exc:
                attempts[mid] = attempts.get(mid, 0) + 1
                log.warning("Could not classify %s (attempt %s): %s", mid, attempts[mid], exc)
                if attempts[mid] < MAX_ATTEMPTS:
                    # Stop here so emails stay in order; the next run retries this one
                    counts["failed"] += 1
                    break
                result = {"label": "QUERY", "confidence": "low", "reason": "Could not classify automatically"}
            low = result["confidence"] == "low"
            if not dry_run:
                mailbox.copy(mid, folders[result["label"]], flag=low)
            store.append_log({**row, **result, "sorted_at": datetime.now(timezone.utc).isoformat()})
            counts[result["label"]] += 1
            attempts.pop(mid, None)

        done[mid] = msg["receivedDateTime"]
        checkpoint = max(checkpoint, received)
        cutoff = (checkpoint - OVERLAP - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        done = {k: v for k, v in done.items() if v >= cutoff}
        state.update(checkpoint=checkpoint.isoformat(), done=done, attempts=attempts, folders=folders, spend=spend)
        store.save_state(state)

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["spend"] = spend
    store.save_state(state)
    log.info("Sorted: %s | Claude this month: %s calls, about $%.2f of $%g budget",
             counts, spend.get("month_calls", 0), spend["month_usd"], MONTHLY_BUDGET_USD)
    return counts


def loop():
    interval = float(os.environ.get("INTERVAL_MINUTES", "5")) * 60
    log.info("Inbox sorter started for %s, checking every %.0f min", os.environ.get("MAILBOX"), interval / 60)
    while True:
        try:
            counts = run()
            # A full batch means more are waiting (catching up after a gap): go again straight away
            if sum(counts.values()) >= int(os.environ.get("MAX_PER_RUN", "40")):
                continue
        except NeedsLogin as exc:
            # Nothing can be done (and nothing is spent) until someone signs in again
            log.error("%s. Checking again in 30 min.", exc)
            time.sleep(1800)
            continue
        except Exception:
            log.exception("Run failed; will try again next cycle")
        time.sleep(interval)


def load_dotenv(path):
    """Read KEY=value lines into the environment, without overriding what is already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # hide one "HTTP Request" line per call
    load_dotenv(HERE / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--login", action="store_true", help="sign in once as the mailbox (prints a code to enter)")
    p.add_argument("--loop", action="store_true", help="keep running and check every INTERVAL_MINUTES")
    p.add_argument("--dry-run", action="store_true", help="classify and print only; copy nothing, save nothing")
    p.add_argument("--since", help="ISO time to start from instead of the saved checkpoint")
    p.add_argument("--max", type=int, help="most emails to handle in this run")
    a = p.parse_args()
    if a.login:
        Mailbox().login()
    elif a.loop:
        loop()
    else:
        print(run(dry_run=a.dry_run, since=a.since, max_messages=a.max))
