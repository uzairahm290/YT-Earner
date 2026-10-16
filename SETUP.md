# Setup guide

Everything here is free. Do part 1 now; part 2 is accounts the later stages need, so you might as well
start them (some take a few days to approve).

---

## Part 1: get stages 1–3 running (about 20 minutes)

### 1. Python environment (Mac)

```bash
cd docu-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Free LLM key

Default provider is Gemini through Google AI Studio's free tier.

1. Go to **aistudio.google.com** → sign in → **Get API key** → create key.
2. Paste it into `.env` as `GEMINI_API_KEY=...`

Backup option: **console.groq.com** → API keys → paste as `GROQ_API_KEY`, then set `llm.provider: groq`
in `config.yaml`. Groq has a smaller context window, so long Wikipedia articles get trimmed more.

Free-tier limits and model names change. If you get a 404 on the model, check the provider's model list
and update `llm.providers.<name>.model` in `config.yaml`.

### 3. Wikipedia user agent

Wikimedia blocks anonymous scrapers. In `.env`:
```
WIKI_USER_AGENT=DocuPipeline/0.1 (contact: your-email@gmail.com)
```

### 4. Check it works

```bash
python -m unittest discover tests        # offline, should print OK
python app.py --demo                     # click around with sample data at http://localhost:8000
```

Then stop it and run it for real:

```bash
python app.py
```

1. **New story** → "Nokia", extra articles "Stephen Elop" and "Symbian" → Start research.
2. On the fact sheet, confirm each flagged claim against the sources and tick it off. If something's wrong, open **Correct the fact sheet** and fix it there.
3. Pick a story angle, go to **Script**, add your own take in the notes box, write the script.
4. Read it out loud. Edit beats in place, fix anything marked in red, save, approve.

The notes box is where your own opinion goes in: that's the part YouTube can't call mass-produced.

### 5. Push to GitHub

Create a **public** repo (public = unlimited free Actions minutes; nothing secret lives in the code).
Then in the repo: **Settings → Secrets and variables → Actions** → add `WIKI_USER_AGENT`.

The `Daily topic shortlist` workflow runs every morning at ~8 AM Pakistan time. Grab the shortlist from
the run's **Artifacts**, or hit **Run workflow** to get one on demand.

---

## Part 2: accounts for the next stages (start now)

| For | What to do | Notes |
|---|---|---|
| **Channel** | Create a YouTube channel (a Brand Account is nicer, so you can add managers later). Country: your real one. Content language: English. | US viewers pay US ad rates no matter where the channel is. |
| **Images/B-roll** | Sign up at **pexels.com/api** and **pixabay.com/api/docs** → put keys in `.env` | Free, commercial use OK. Wikimedia Commons needs no key. |
| **GPU** | **kaggle.com** → Settings → **phone verification** (needed for GPU + internet in notebooks) → Create New Token → saves `kaggle.json` | Used for voice and, optionally, a local LLM. |
| **Upload** | **console.cloud.google.com** → new project → enable **YouTube Data API v3** → OAuth consent screen (External) → OAuth client (Desktop app) | Until Google audits the project, API uploads stay **private**. Fine: you publish manually after review. Apply for the audit once you have a few videos. |
| **Music** | Nothing to sign up for. Use the **YouTube Audio Library** in YouTube Studio | Safe to monetize. |

---

## Your weekly routine (once everything's built)

1. Open the dashboard, glance at Topic ideas, start 1–2 stories with real drama.
2. Spend ~30 min on each fact sheet's checklist. Accuracy is your brand and your legal cover.
3. Write the script with your angle and notes, edit for 20–30 min, approve.
4. The rest of the pipeline renders the long video and its Shorts; you watch the cut and approve it on the dashboard.
5. It uploads privately; you schedule for **2–6 PM Eastern** (midnight–4 AM in Pakistan).

---

## Roadmap

| # | Stage | Plan |
|---|---|---|
| 4 | **Voice** | Kokoro-82M (Apache 2.0, commercial OK, American voices, runs on CPU), one WAV per beat, mastered with FFmpeg (EQ, compression, room tone) |
| 5 | **Visuals** | Per-beat fetch: Wikimedia Commons for `archive_photo`, Pexels/Pixabay for `broll`, Python-rendered `chart`/`map`/`title_card`/`document` cards. License + credit saved per asset |
| 6 | **Render** | MoviePy/FFmpeg: Ken Burns on stills, cuts timed to the voice, lower-thirds, captions, grade + grain, music ducked under narration |
| 7 | **Shorts** | Re-cut the flagged beat ranges vertical 9:16, new hook line, bigger captions |
| 8 | **Approval** | Final-cut review on the dashboard: watch it, approve or send back |
| 9 | **Upload** | YouTube Data API: private upload with title/description/tags/chapters, then scheduled publish |
| 10 | **Orchestration** | GitHub Actions ties it together; Kaggle only gets called if a stage needs a GPU |

## Ground rules baked into this setup

- **No AI images.** Real photos, real footage, real documents, honest charts.
- **No scraped news photos.** Agency photos (AP, Reuters, Getty) get copyright strikes. Stick to Commons/Pexels/Pixabay/public-domain US government archives.
- **No cloned voices of real people.** Stock Kokoro voice or your own.
- **Every number traces to a source.** The lint enforces it; you check the sources.
