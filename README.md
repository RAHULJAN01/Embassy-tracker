# 🏛️ Embassy Procurement Tracker

Watches US-embassy procurement pages **once a day, for free**, and emails you
only when a **new solicitation** or an **amendment/change** appears. Runs by
itself on GitHub — no computer needs to stay on, nothing to pay.

It catches everything posted on an embassy's public procurement page.
Solicitations sent **only by direct email** won't show here — for those, get on
each embassy's GSO vendor list (separate task).

---

## What you'll set up (about 15 minutes, one time)

You need two free things: a **Gmail App Password** (so the tracker can send you
mail) and a **GitHub account** (where it runs). Follow in order.

### STEP 1 — Get a Gmail App Password
1. Use any Gmail account. Turn on **2-Step Verification**:
   Google Account → **Security** → **2-Step Verification** → turn it on.
2. Then open **https://myaccount.google.com/apppasswords**
3. App name: type `Embassy Tracker` → **Create**.
4. Google shows a **16-letter password** (like `abcd efgh ijkl mnop`).
   Copy it and remove the spaces → `abcdefghijklmnop`. Keep it for Step 4.
   *(This is NOT your normal Gmail password. It only lets this app send mail.)*

### STEP 2 — Make a GitHub account + repository
1. Sign up free at **https://github.com/signup**
2. Top-right **+** → **New repository**.
3. Repository name: `embassy-tracker` → set it **Private** → **Create repository**.

### STEP 3 — Upload these files
1. On your new empty repo page, click **“uploading an existing file”**.
2. Drag in **everything from this folder** — `tracker.py`, `sites.yaml`,
   `requirements.txt`, `README.md`, and the `.github` and `state` folders.
   *(Easiest: select all and drag. Keep the folder structure.)*
3. Click **Commit changes**.

### STEP 4 — Add your 3 secrets (your email login, kept hidden)
In the repo: **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Add these three, one at a time:

| Name | Value |
|---|---|
| `GMAIL_USER` | the Gmail address that sends the alert, e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | the 16-letter password from Step 1 (no spaces) |
| `ALERT_TO` | where alerts go (can be the same address; commas for more than one) |
| `SAM_API_KEY` | **(recommended)** free SAM.gov key — turns on the all-embassies SAM feed. See below. |

### STEP 4b — Get your free SAM.gov API key (covers ALL embassies)
This is what pulls every Department-of-State solicitation worldwide into the digest.
1. Sign in at **https://sam.gov** (make a free account if needed).
2. Top-right → your name → **Account Details**.
3. Find **"API Key"** → **Generate/Request** a Public API Key. Copy it.
4. Add it as the secret **`SAM_API_KEY`** (Step 4).
Without this key the tracker still works — it just watches the embassy pages only.

### STEP 5 — Turn it on and test it now
1. Open the **Actions** tab. If it asks, click **“I understand… enable workflows.”**
2. Click **Embassy Tracker** (left) → **Run workflow** → **Run workflow**.
3. Wait ~1 minute. You should get a **“Baseline captured”** email listing what's
   currently open on each page. That confirms it works.

**Done.** From now on it runs **every day automatically** and emails you only
when something new or amended shows up.

---

## Add more embassies (do this any time)
Open **`sites.yaml`** in your repo → pencil ✏️ **Edit** → copy a block, paste,
change the `name` and `url` → **Commit changes**. That's it. Send me a batch of
embassy names and I'll hand you ready-to-paste blocks.

## Common tweaks
- **Change the daily time:** edit `.github/workflows/track.yml`, the `cron` line
  (`0 6 * * *` = 06:00 UTC). [crontab.guru](https://crontab.guru) helps.
- **Want a daily “all clear” email too** (proof it ran on quiet days): in the same
  file, remove the `#` in front of `SEND_DAILY_DIGEST: "1"`.
- **A page won't load** (embassy blocks bots / heavy JavaScript): the email lists
  it under **“Could not check”** so you never silently miss one — open that one by
  hand. Tell me which, and I'll wire a workaround.

## What each file does
- `tracker.py` — the checker (fetch → find solicitations → compare → email).
- `sites.yaml` — your list of embassy pages. **This is the file you'll edit.**
- `.github/workflows/track.yml` — the daily timer that runs it on GitHub.
- `state/` — memory of what each page looked like last time (auto-updated).
- `requirements.txt` — the libraries GitHub installs automatically.
