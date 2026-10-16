# docu-pipeline

A free, mostly-automated pipeline for a tech documentary YouTube channel aimed at a US audience.
The machine does the grunt work. You pick the story, check the facts, and sign off on the cut.
That human step is what keeps the channel on the right side of YouTube's inauthentic-content rules.

```
topics ─► research ─► script ─► voice ─► visuals ─► render ─► shorts ─► upload
  ✅         ✅          ✅       next     next      next      next      next
```

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
cli.py
tests/               offline tests + fixtures (DOCU_MOCK_LLM=1 replays canned LLM replies)
.github/workflows/   daily topic shortlist + tests
```
