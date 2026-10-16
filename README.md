# docu-pipeline

A free, mostly-automated pipeline for a tech documentary YouTube channel aimed at a US audience.
The machine does the grunt work. You pick the story, check the facts, and sign off on the cut.
That human step is what keeps the channel on the right side of YouTube's inauthentic-content rules.

```
topics ─► research ─► script ─► voice ─► visuals ─► render ─► shorts ─► upload
  ✅         ✅          ✅       next     next      next      next      next
```

## The dashboard

```bash
python app.py          # http://localhost:8000
python app.py --demo   # sample data, no keys needed, good for a first look
```

Everything runs from the Production Desk:

- **Desk**: every story sorted by what it needs from you next (check facts, write script, approve).
- **Topic ideas**: today's shortlist, one click to start research.
- **Fact sheet**: timeline, people, numbers with their sources, a checklist of claims to confirm, and a place to correct anything wrong.
- **Script**: a two-column picture/narration paper edit. Running timecodes, shot type and search for each beat, lower thirds, pauses. AI phrases get marked in red as you type; numbers that aren't in the fact sheet get marked on save. Approve is locked until it's clean.
- **Shorts**: phone-shaped previews of each Short with its hook line.
- **Title and thumbnail**: pick or write the title, thumbnail text and description.

The CLI below does the same things if you'd rather script it.

## What works today

| Stage | Command | Output (in `episodes/<slug>/`) |
|---|---|---|
| Topics | `python cli.py topics` | shortlist from US tech RSS + Wikipedia traffic spikes |
| Research | `python cli.py research "Nokia" --extra "Stephen Elop"` | `factsheet.md`: timeline, people, numbers, every fact tagged with its source, plus a ⚠️ verify list |
| Script | `python cli.py script nokia --angle "..." --notes "..."` | `script.md` to read and edit, `script.json` for the next stages |

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
  episode.py         per-video folder + status
app.py               dashboard server (FastAPI)
web/                 dashboard frontend (plain HTML/CSS/JS, no build step)
cli.py
tests/               offline tests + fixtures (DOCU_MOCK_LLM=1 replays canned LLM replies)
.github/workflows/   daily topic shortlist + tests
```
