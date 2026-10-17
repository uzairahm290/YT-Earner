# Deploying to Render + Supabase (free)

About 20 minutes. At the end you'll have the Production Desk at `https://production-desk-xxxx.onrender.com`,
behind a password, with every story stored in Supabase.

```
your phone / laptop ──► Render (free web service, runs app.py)
                            │
                            ├──► Supabase Postgres (stories, fact sheets, scripts)
                            ├──► Gemini API (research + scripts)
                            └──► Wikipedia + RSS
GitHub Actions (daily) ─────► Supabase (topic shortlist)
```

## What to expect from the free tier

- **Render sleeps after ~15 minutes with no visits.** The first visit after that takes about a minute to wake up.
  While the dashboard is open it keeps itself awake (it checks on running jobs every couple of seconds), so
  start a research or script job and leave the tab open until it's done.
- **If Render restarts mid-job, the job is lost, not your story.** The story shows "Research didn't finish"
  with a button to run it again.
- **Supabase pauses free projects after about a week with no activity.** The daily topics workflow writes to
  the database every morning, which counts as activity.
- The render stage (FFmpeg) will need more muscle than Render's free tier. That's a later problem.

---

## 1. Supabase: the database

1. Go to **supabase.com** → sign in with GitHub → **New project**.
2. Name it `production-desk`. Set a strong database password and **save it somewhere**, because you'll need it in a second.
   Region: **Southeast Asia (Singapore)**, to sit next to the Render server.
3. When the project is ready, click **Connect** (top of the dashboard) → **Session pooler** → copy the URI. It looks like:
   ```
   postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
   ```
   Replace `[YOUR-PASSWORD]` with your database password.
   - Use the **Session pooler** string, not "Direct connection". Direct connections need IPv6, which Render doesn't have.
   - If your password has symbols like `@ # / ? %`, they break the URL. Easiest fix: reset the database
     password (Project Settings → Database) to letters and numbers only.

You don't need to create any tables. The app creates its own `desk.episode_files` table on first start.
It lives in its own `desk` schema on purpose: Supabase publishes the `public` schema through its REST API,
and your scripts shouldn't be readable by anyone holding the project's public key.

**Test it from your Mac first** (catches a bad URL before Render does):
```bash
# in .env
DATABASE_URL=postgresql://postgres.abcdefghijkl:yourpassword@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres

python cli.py list           # "No episodes yet." = connected
python cli.py push-local     # optional: copy stories you already made locally into Supabase
```

## 2. GitHub: the repo

Already done: the code lives at **github.com/uzairahm290/YT-Earner**.
To work on it from your Mac:

```bash
git clone https://github.com/uzairahm290/YT-Earner.git
cd YT-Earner
cp .env.example .env    # fill in your keys; .env is gitignored and never gets pushed
```

## 3. Render: the server

1. **render.com** → sign in with GitHub.
2. **New → Blueprint** → pick the `YT-Earner` repo. Render finds `render.yaml` and shows one service, `production-desk`.
3. It asks for four values:

   | Key | Value |
   |---|---|
   | `DESK_PASSWORD` | A long password you'll type to sign in. Not your Supabase password. |
   | `DATABASE_URL` | The Session pooler URI from step 1 |
   | `GEMINI_API_KEY` | Your AI Studio key |
   | `WIKI_USER_AGENT` | `DocuPipeline/0.1 (contact: your-email@gmail.com)` |

4. **Apply**. The first build takes a few minutes. When the log shows `Uvicorn running`, open the URL at the top of the page.

You should land on the sign-in screen. The app refuses to start on Render without `DESK_PASSWORD`,
so it can never end up on the internet unprotected by accident.

Every `git push` to `main` redeploys automatically.

## 4. GitHub Actions: the worker and daily topics

Narration (and later visuals and rendering) is too heavy for Render's free instance, so it runs on
free GitHub Actions machines. The dashboard queues the job in Supabase; the **Production worker**
workflow picks it up. It needs the database address to do that.

Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `DATABASE_URL`: the same Session pooler URI that's on Render
- `WIKI_USER_AGENT`: `DocuPipeline/0.1 (contact: your-email@gmail.com)`

Then **Actions → Production worker → Run workflow** once. It should finish green with "0 queued".
After that it checks for work every 10 minutes by itself, and the daily topic shortlist starts
landing in the dashboard's Topic ideas.

### Optional: start narration instantly

Without this, a queued narration starts within ~15 minutes. With it, it starts immediately.

1. github.com → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. Repository access: **Only select repositories → YT-Earner**
3. Permissions → Repository permissions → **Actions: Read and write** (nothing else)
4. Generate, copy, and add it on Render as `GITHUB_DISPATCH_TOKEN`

GitHub pauses scheduled workflows in repos with no commits for 60 days. If narration stops
getting picked up after a quiet stretch, open Actions and re-enable the workflow.

## 5. Optional: stock footage for B-roll

Without a key, B-roll beats ("factory floor at night") use Wikimedia Commons photos. With one, they get real
stock video. Both are free:
- **Pexels**: pexels.com/api → sign up → copy the key → add `PEXELS_API_KEY` on Render
- **Pixabay** (backup source): pixabay.com/api/docs → your key is on that page when logged in → `PIXABAY_API_KEY`

Then hit **Search again** on a story's Pictures tab.

## 6. Optional: AI illustrations on Kaggle

For abstract beats only (see the README for the rules). Uses Kaggle's free GPU, about 10–15 minutes per batch.

1. kaggle.com → **Settings → Phone verification** (Kaggle needs it for GPU and internet in notebooks)
2. **Settings → API → Create New Token** → copy the `KGAT_…` token
3. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `KAGGLE_USERNAME`: your Kaggle username (the one in your profile URL)
   - `KAGGLE_API_TOKEN`: the token

Then use **✦ Illustrate** on a story's Pictures tab (or ✦ on a single picture). The worker pushes a
private notebook called `docu-illustrate-<story>` to your Kaggle account and runs it there; you can
open it on kaggle.com/code to watch. If Kaggle's GPU queue is slow, the batch keeps drawing there and
a later worker run collects it.

## 7. Optional: one-click YouTube uploads

Without this, the YouTube tab still gives you an upload kit (files + title, description with
chapters and credits, tags) to paste into YouTube Studio. With it, each video is one click.

1. console.cloud.google.com → create a project (any name, e.g. "Production Desk")
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - User type **External**, app name "Production Desk", your email in both email fields
   - Scopes: you can skip this page
   - Test users: add the Google account that owns your channel
   - Then **Audience → Publish app** (to "In production"). In Testing mode Google cuts the connection
     every 7 days. You don't need Google's verification for your own use; you'll just see a
     "Google hasn't verified this app" screen when connecting: **Advanced → Go to Production Desk**.
4. **Credentials → Create credentials → OAuth client ID** → type **Web application**
   - Authorized redirect URI: the one shown on the YouTube tab when you're not connected yet,
     which is your Render address + `/api/youtube/callback`
     (e.g. `https://production-desk.onrender.com/api/youtube/callback`)
5. Copy the **Client ID** and **Client secret** and add both, with the same names, in two places:
   - Render → your service → **Environment**: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
   - GitHub repo → **Settings → Secrets and variables → Actions**: the same two
6. Open a rendered story → **YouTube** tab → **Connect YouTube channel** → pick the channel's account.

**Private until Google's review.** YouTube locks videos uploaded through a new, unreviewed API app to
private, and a locked video can't be made public later. The review (an "audit") is free:
support.google.com/youtube/contact/yt_api_form. Until it passes, upload anything that should go
public with the upload kit, and use the button for private drafts.

Free quota: 10,000 units a day; an upload costs 1,600 and a caption track 400, so about 6 uploads a day.

## When something's off

| Symptom | Likely cause |
|---|---|
| Build fails on `psycopg` | Check `PYTHON_VERSION` is `3.12.7` in the service's Environment tab |
| Log says `DESK_PASSWORD is not set` | Add it under Environment, then **Manual Deploy** |
| `connection refused` / `Network is unreachable` to Supabase | You used the Direct connection string. Switch to Session pooler |
| `password authentication failed` | Wrong DB password in the URL, or a symbol in it needs URL-encoding |
| Sign-in page loads forever | Render is waking up. Give it a minute |
| "Research didn't finish" | Check Render's Logs tab. Usually a missing/wrong `GEMINI_API_KEY` or the free-tier rate limit |
| Changing the password | Update `DESK_PASSWORD` on Render. Every device gets signed out |
| Narration stuck on "queued" | Actions → Production worker: is it failing? Usually the `DATABASE_URL` secret is missing |
| Narration failed | The error shows on the story page. The worker's log in Actions has the details |
| "YouTube withdrew the channel connection" | The Google app is still in Testing (7-day logins). Publish it (step 7.3), then connect again |
| Upload says the video "has expired" | GitHub keeps rendered videos 30 days. Render it again, then upload |
