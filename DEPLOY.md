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

## 2. GitHub: push the repo

```bash
cd docu-pipeline
git remote add origin https://github.com/<you>/docu-pipeline.git
git push -u origin main
```
Create the empty repo on github.com first (no README, no .gitignore, since the repo already has them).
Public or private both work with Render. Public gets unlimited free GitHub Actions minutes.

Check that `.env` is **not** in the repo (it's in `.gitignore`). Your keys go into Render, never into git.

## 3. Render: the server

1. **render.com** → sign in with GitHub.
2. **New → Blueprint** → pick the `docu-pipeline` repo. Render finds `render.yaml` and shows one service, `production-desk`.
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

## 4. GitHub Actions: daily topics into the dashboard

Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `DATABASE_URL`: the same Session pooler URI
- `WIKI_USER_AGENT`: same as above

Then **Actions → Daily topic shortlist → Run workflow** once to test. The shortlist should appear under
**Topic ideas** in the dashboard.

---

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
