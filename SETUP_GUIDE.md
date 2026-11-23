# Inbox sorter and mailbox dashboard: setup guide

**What it does:** checks the research.smallcase@niveshaay.com Inbox every 5 minutes. It uses Claude to decide whether each new client email is a **complaint** or a **query**, then puts a copy in the `Complaints` or `Queries` folder. The original stays in the Inbox. If the server is down for a while, the sorter works through everything it missed, oldest first, as soon as it is back.

**It also runs the mailbox dashboard:** once a day it saves every new email in Inbox, Sent Items and Archive to a permanent record on the server. It rebuilds the reply-time numbers and serves the dashboard on a password-protected web address. Click any number to read the emails behind it. The **Weekly review** tab shows how any finished week (Sunday to Saturday), or run of weeks, went: topics, stocks and baskets clients asked about, and content ideas. New conversations are tagged for it by Claude Haiku, about ₹5–20 a month, capped by `ENRICH_MONTHLY_USD` (default $2). Set `ENRICH_ENABLED=0` to switch tagging off.

**Where it runs:** as a small always-on app on our Easypanel server.

**Setup takes three parts:**
1. Microsoft 365: register the app and get one admin click (done, 17 Nov 2026).
2. Optional: test it on a laptop without changing anything (10 minutes).
3. Easypanel: deploy it and sign in once (15 minutes).

---

## Part 1: Microsoft 365 (done, 17 Nov 2026)

The sorter signs in **as the research.smallcase@ mailbox itself**, using a one-time code sign-in. It can only reach that mailbox, because that is the account it signs in as.

What was set up, for reference:

1. **entra.microsoft.com → App registrations → `Niveshaay Inbox Sorter`**: single tenant, no redirect URI.
   - `CLIENT_ID` = Application (client) ID, `TENANT_ID` = Directory (tenant) ID.
2. **Authentication → Allow public client flows = Yes.** This is what allows the code sign-in.
3. **API permissions → Microsoft Graph → Delegated → `Mail.ReadWrite`** (plus the default `User.Read`).
4. **A Global Admin clicked "Grant admin consent for Niveshaay Investment Advisors".** It is needed only once, because the company does not let users approve apps themselves.
5. **No client secret is used.** Leave `CLIENT_SECRET` empty. If one was created in Certificates & secrets, it can be deleted.

Optional hardening: Enterprise applications → `Niveshaay Inbox Sorter` → Properties → **Assignment required = Yes**. Then under Users and groups, add only research.smallcase@.

### Signing in

Run `python sorter.py --login`. It prints a code and a link: open the link, enter the code, and sign in as **research.smallcase@niveshaay.com**. The sign-in is saved in `signin_cache.bin` in the data folder and renews itself while the sorter keeps running.

You need to sign in again only if the data folder is lost, the mailbox password is changed, or the sign-in goes unused for about 90 days. When that happens, the app's Logs show `Not signed in, or the sign-in expired` every 30 minutes.

---

## Part 2: Optional test on a laptop (nothing is copied)

This needs Python 3.10 or newer, and the settings in `cloud/.env`.

```powershell
cd cloud
pip install -r requirements.txt
python sorter.py --login          # first time only
python sorter.py --dry-run --since 2026-11-04T00:00:00Z --max 10
```

It prints how each of 10 Inbox emails would be sorted, without copying anything. It costs about ₹10–15 in Claude API usage.

---

## Part 3: Deploy on Easypanel

1. The code is in GitHub (`niveshaaylearning-EB/email_tracker`). The `.gitignore` keeps `data/` and `.env` out of it.
2. In Easypanel, open your project and choose **+ Service → App**.
   - Name: `inbox-sorter`
   - **Source:** GitHub, the repository above, branch `main`, build path `/`.
   - **Build:** Dockerfile
3. **Environment:** paste these values (`.env.example` lists them all):

   ```
   TENANT_ID=...
   CLIENT_ID=...
   CLIENT_SECRET=
   MAILBOX=research.smallcase@niveshaay.com
   ANTHROPIC_API_KEY=...
   START_FROM=2026-11-24T03:30:00Z
   INTERVAL_MINUTES=5
   DASHBOARD_PASSWORD=<a long password>
   REFRESH_HOURS=24
   ```

   `START_FROM` is used only on the very first run. It is the point from which emails get sorted, so set it to the go-live date. Leave it empty to start from the moment it is deployed.
4. **Mounts:** add a **Volume**:
   - Name: `inbox-sorter-data`
   - Mount path: `/data`

   Progress is saved here. Without the volume, every redeploy would start again from `START_FROM`.
5. **Domains:** add a domain (for example `mailbox.<your domain>`) pointing to port **8080**, with HTTPS on. That is the dashboard address. It asks for `DASHBOARD_PASSWORD`; the user name can be anything unless you set `DASHBOARD_USER`.
6. Click **Deploy**. The first time, the Logs show `Not signed in, or the sign-in expired`. To sign in:
   - Open the app's **Console** (terminal) in Easypanel.
   - Run `python sorter.py --login`.
   - Enter the code at the link it shows, signing in as research.smallcase@.

   The sign-in is saved on the `/data` volume, so redeploys keep it. Then restart the app.
7. Open **Logs**. You should see:

   ```
   Inbox sorter started for research.smallcase@niveshaay.com, checking every 5 min
   Sorted: {'COMPLAINT': 0, 'QUERY': 3, 'skipped': 2, 'failed': 0}
   Inbox: 2650 emails since 2026-07-01
   Tracker: fetched 4100, new 4100, dashboard {...}
   ```

### Load the older history once

Outlook moves older mail into the online archive, which the app can't read. To keep the history from 23 Jul 2026 on the dashboard:

1. Open the dashboard address and choose **Admin**.
2. Under **Add the older history**, upload `data/emails_master.jsonl` from the laptop's Email tracker folder.

Emails already stored are never replaced. After this, the server keeps everything itself, including mail that later moves to the archive.

### Check it works

1. From a personal email address, send: *"I had already cancelled my mandate but ₹4,999 was still debited. Please refund."* Within 5 minutes a copy should appear in **Complaints**.
2. Send: *"What is the renewal price for Green Energy?"* A copy should appear in **Queries**.
3. Both originals stay in the Inbox.

---

## Day to day

| Situation | What happens / what to do |
|---|---|
| Server restarts or is down for a day | When it is back, the sorter works through the missed emails oldest first, 40 at a time, until it has caught up |
| Claude is unsure | The copy is **flagged** in the folder so someone checks it |
| Claude cannot be reached | The sorter retries on the next check. After 3 failures for the same email, it puts the copy in Queries, flagged |
| A copy is in the wrong folder | Drag it to the right one. Tell Claude about repeated mistakes so the rules can be adjusted |
| Change the sorting rules | Edit the `system` text in `base_request.json`, push to GitHub, redeploy |
| Pause sorting | Stop the app in Easypanel. When you start it again, it catches up |
| Logs say `Not signed in, or the sign-in expired` | Open the Console in Easypanel, run `python sorter.py --login`, and sign in as research.smallcase@. Nothing is lost: it catches up afterwards |
| Dashboard data looks old | The top of the dashboard shows when it was last refreshed. **Admin → Refresh now** collects new mail straight away |
| Change the dashboard password | Change `DASHBOARD_PASSWORD` in Easypanel and redeploy |
| Where the email record lives | `/data/tracker/emails_master.jsonl` on the volume holds every email collected, and is only ever added to. Back up the volume like any other company data |
| See what was sorted | `/data/log/YYYY-MM.jsonl` on the volume holds one line per email, with label, confidence and reason |

**What gets skipped (stays in the Inbox only):** our own emails, internal @niveshaay.com mail, no-reply and automated senders, smallcase ticket acknowledgements and feedback surveys.

**Cost and limits:** the Claude API is billed per use. Built-in guards keep spending under control:
- `DAILY_LIMIT`: the most Claude calls per day (default 100).
- `MONTHLY_BUDGET_USD`: the estimated monthly spend (default $15).

Past either limit, emails are still copied, to **Queries** and flagged, but Claude is not called. Each email's token use and estimated cost are written to the sort log, and the app's Logs show the running monthly total. Also set a monthly **spend limit** in the Anthropic Console (Settings → Limits) as a hard stop. The Easypanel server cost doesn't change.

**Data:** each email's sender, subject and text go to Anthropic's API to be sorted. Compliance should be aware of this.
