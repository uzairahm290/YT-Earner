# docu-pipeline

A free, mostly-automated pipeline for a tech documentary YouTube channel aimed at a US audience.
The machine does the grunt work. You pick the story, check the facts, and sign off on the cut.
That human step is what keeps the channel on the right side of YouTube's inauthentic-content rules.

```
topics ─► research ─► script ─► voice ─► visuals ─► render ─► shorts ─► upload
  ✅         ✅          ✅        ✅        ✅        ✅       next      next
```

## The dashboard

```bash
python app.py          # http://localhost:8000
python app.py --demo   # sample data, no keys needed, good for a first look
INLINE_WORKER=1 python app.py   # run narration inside the dashboard (pip install -r requirements-worker.txt)
```

Everything runs from the Production Desk:

- **Desk**: every story sorted by what it needs from you next (check facts, write script, approve).
- **Topic ideas**: today's shortlist, one click to start research.
- **Fact sheet**: timeline, people, numbers with their sources, a checklist of claims to confirm, and a place to correct anything wrong.
- **Script**: a two-column picture/narration paper edit. Running timecodes, shot type and search for each beat, lower thirds, pauses. AI phrases get marked in red as you type; numbers that aren't in the fact sheet get marked on save. Approve is locked until it's clean.
- **Narration**: approving a script queues it for the narrator (Kokoro-82M, 7 voices to pick from). When it's done, every beat gets a play button and the script follows along as it plays. Edit the script and the recording is marked out of date.
- **Pictures**: a real picture for every beat. Photos from Wikimedia Commons (freely licensed only, credited), stock footage from Pexels/Pixabay when a key is set, and clean cards for figures and titles. Documents get a card flagged for you to upload a screenshot of the real thing. Pick alternates, search again in your own words, or upload your own. Credits are collected for the description.
- **AI illustrations (optional)**: for abstract B-roll beats only ("gears grinding to a halt", "two paths diverging"), SDXL draws options on Kaggle's free GPU in one fixed ink-and-duotone style, so they read as illustration, never as photos. Beats that name a person, product, company or date are never eligible. Every one is labeled AI and only used if you pick it.
- **Render**: one click cuts the 1080p video on the GitHub worker: Ken Burns moves on photos, stock clips trimmed to the beat, cards in the channel's type, lower thirds, one grade with light grain, narration mastered on top, plus an SRT caption file. A 60-second preview plays in the dashboard; the full MP4 downloads from the GitHub run (kept 30 days).
- **Shorts**: phone-shaped previews of each Short with its hook line.
- **Title and thumbnail**: pick or write the title, thumbnail text and description.

The CLI below does the same things if you'd rather script it.

To put it online behind a password (Render + Supabase, free), see **[DEPLOY.md](DEPLOY.md)**.

## What works today

| Stage | Command | Output (in `episodes/<slug>/`) |
|---|---|---|
| Topics | `python cli.py topics` | shortlist from US tech RSS + Wikipedia traffic spikes |
| Research | `python cli.py research "Nokia" --extra "Stephen Elop"` | `factsheet.md`: timeline, people, numbers, every fact tagged with its source, plus a ⚠️ verify list |
| Script | `python cli.py script nokia --angle "..." --notes "..."` | `script.md` to read and edit, `script.json` for the next stages |
| Voice | `python cli.py voice nokia --voice am_onyx` | one mastered MP3 (-16 LUFS) + start/end time of every beat |
| Pictures | dashboard → Pictures tab | `visuals.json`: candidates, your pick and the credit for every beat (files download at render time) |
| Render | dashboard → Render the video | `output/<slug>.mp4` + `.srt`, uploaded as a GitHub Actions artifact |
| Worker | `python cli.py worker` | runs whatever the dashboard queued; this is what GitHub Actions calls |

The script is broken into **beats**: one beat = 1–3 sentences of narration plus the exact shot to cut to
(real archive photo, stock B-roll, a document on screen, a chart, a map, a title card). It also flags
the moments worth cutting into Shorts, and suggests titles, thumbnail text, description and tags.

A lint pass runs on every script and flags:
- AI-sounding phrases ("let's dive in", "delve", "game-changer"... edit the list in `config.yaml`)
- **numbers or years that don't appear in the fact sheet**: the cheapest hallucination check there is
- length way off the target

## Quick start

See **[SETUP.md](SETUP.md)** for accounts and keys. Short version:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add GEMINI_API_KEY (free) + your contact in WIKI_USER_AGENT
python -m unittest discover tests # offline, no key needed
python cli.py topics
python cli.py make "Nokia" --extra "Stephen Elop" "Symbian"
```

## Layout

```
config.yaml          channel voice, LLM provider, feeds, banned phrases, script length
prompts/             the research and script prompts (tune these, they matter most)
pipeline/
  topics.py          RSS + Wikipedia trending
  research.py        Wikipedia → sourced fact sheet
  script.py          fact sheet → beats + visuals + Shorts picks, with lint
  llm.py             any OpenAI-compatible endpoint (Gemini / Groq / local)
  voice.py           script → Kokoro narration → FFmpeg mastering → MP3 + beat timings
  visuals.py         beat → Commons photos / Pexels+Pixabay video / cards, licenses and credits
  illustrate.py      abstract beats → SDXL on Kaggle (house style) → AI options; the eligibility rule lives here
  render.py          shots → Ken Burns / clips / cards + lower thirds → graded 1080p MP4 + captions
  worker.py          job queue: claims queued episodes, runs them, records errors
  episode.py         per-video files + status
  store.py           where files live: episodes/ folder locally, Supabase when DATABASE_URL is set
app.py               dashboard server (FastAPI) + login
render.yaml          Render blueprint
web/                 dashboard frontend (plain HTML/CSS/JS, no build step)
assets/fonts/        Archivo + Courier Prime (SIL Open Font License) for cards and lower thirds
cli.py
tests/               offline tests + fixtures (DOCU_MOCK_LLM=1 replays canned LLM replies)
.github/workflows/   production worker (every 10 min), daily topic shortlist, tests
```
