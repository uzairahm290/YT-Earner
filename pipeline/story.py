"""Story format: long first-person stories (workplace revenge and the like), told over stock footage.

No research stage. A premise goes in; the LLM writes an outline (title options, hook, chapters),
then each chapter in turn with the story so far in view, so the plot holds together over 20+
minutes. Every paragraph comes with a short scene description ("open-plan office at night",
"car parked in rain") that the Pictures stage uses to find a matching stock clip.

The result is an ordinary script.json (chapters of beats), so voice, pictures, Shorts and upload
work unchanged; render.py hands story episodes to story_render.py for the overlay layout.

Guardrails, because YouTube demonetizes mass-produced copies:
  - every premise is compared with the channel's earlier ones; close repeats are flagged
  - descriptions always say the story is dramatized fiction
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ValidationError

from .config import load_config
from .episode import Episode
from .llm import LLMError, chat, extract_json
from .script import Beat, Chapter, Script, Visual, lint, render_script
from .store import get_store

SETTINGS = "_settings"
DISCLAIMER = ("This story is fiction, dramatized for entertainment. Names, companies and events are invented; "
              "any resemblance to real people is coincidental. We don't encourage revenge in real life.")


def cfg() -> dict:
    return load_config()["story"]


# ──────────────────────────── shapes the LLM must return ────────────────────────────

class OutlineChapter(BaseModel):
    title: str
    summary: str


class Outline(BaseModel):
    title_options: list[str] = Field(min_length=1)
    thumbnail_text_options: list[str] = Field(default_factory=list)
    hook: str
    chapters: list[OutlineChapter] = Field(min_length=4)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class Paragraph(BaseModel):
    text: str
    scene: str


class ChapterText(BaseModel):
    paragraphs: list[Paragraph] = Field(min_length=2)


# ──────────────────────────── prompts ────────────────────────────

STYLE = """Voice: first person, spoken out loud by {narrator}, a calm, sharp, dry-humoured American in his
late 30s telling a friend what happened. Past tense. Plain words. Specific details (a badge reader that
beeps twice, a coffee going cold, a Tuesday 4:47 pm email). Short sentences when tension rises.
Dialogue is quoted and brief. No narration about "this story" or "in this video". Never address
the viewer as "you guys". Never write: {banned}.
Everything is fiction: invented names, invented companies (never a real brand or real person)."""


def _style() -> str:
    return STYLE.format(narrator=cfg()["narrator"],
                        banned=", ".join(f'"{p}"' for p in load_config()["banned_phrases"]))


def outline_prompt(premise: str, minutes: int, notes: str | None) -> tuple[str, str]:
    c = cfg()
    system = f"""You write long-form narrated stories for a YouTube channel in the niche: {c['niche']}.
{_style()}

Plan one story of about {minutes} minutes ({minutes * c['words_per_minute']} spoken words) as
{c['chapters']} chapters. Structure: the hook (one or two lines that open the video and make leaving
impossible), the setup and the injustice, the narrator quietly preparing, escalation, the turn where
his preparation pays off, consequences for the villain, and a short closing reflection.
The narrator wins through competence, paperwork and consequences people earned: never violence,
never anything illegal.

Return JSON only:
{{"title_options": [3 YouTube titles, under 90 characters, first person, e.g. "Fired For 'Non-Essential' License, I Triggered A Corporate Shutdown"],
 "thumbnail_text_options": [3 options of 2-4 words],
 "hook": "...",
 "chapters": [{{"title": "...", "summary": "3-5 sentences of what happens"}}],
 "description": "2 short paragraphs for YouTube, no spoilers of the ending",
 "tags": [10-15 tags]}}"""
    user = f"PREMISE: {premise}" + (f"\n\nCREATOR NOTES: {notes}" if notes else "")
    return system, user


def chapter_prompt(outline: Outline, index: int, so_far: str, words: int) -> tuple[str, str]:
    c = cfg()
    system = f"""You are writing one chapter of a narrated story for YouTube ({c['niche']}).
{_style()}

Write chapter {index + 1} of {len(outline.chapters)} in about {words} words, as paragraphs of 50-110
words. Continue seamlessly from the story so far; don't repeat it and don't summarize it.
{"Open the whole video with the hook, word for word, then go straight into the story." if index == 0 else ""}
{"This is the last chapter: land the ending, then two or three lines of quiet reflection. No call to subscribe." if index == len(outline.chapters) - 1 else "End the chapter on something that makes the next one necessary."}

For each paragraph add "scene": 3-6 plain words for a stock video clip that matches the mood or setting
(e.g. "office hallway fluorescent lights", "city traffic at night", "rain on car window",
"server room blinking lights"). Generic settings only: no names, no brands, no faces in close-up.

Return JSON only: {{"paragraphs": [{{"text": "...", "scene": "..."}}]}}"""
    plan = "\n".join(f"{i + 1}. {ch.title}: {ch.summary}" for i, ch in enumerate(outline.chapters))
    user = (f"TITLE: {outline.title_options[0]}\nHOOK: {outline.hook}\n\nPLAN:\n{plan}\n\n"
            f"STORY SO FAR (last part):\n{so_far[-6000:] or '(nothing yet: this is the opening)'}\n\n"
            f"WRITE CHAPTER {index + 1}: {outline.chapters[index].title}")
    return system, user


def _ask(system: str, user: str, model, task: str):
    last = None
    for attempt in range(3):
        prompt = user if attempt == 0 else f"{user}\n\nYOUR PREVIOUS REPLY WAS INVALID: {last}\nReturn the full corrected JSON."
        try:
            return model.model_validate(extract_json(chat(system, prompt, task=task)))
        except (ValidationError, LLMError) as e:
            last = str(e)[:800]
            print(f"  ! {task} reply invalid (attempt {attempt + 1}), asking again")
    raise LLMError(f"The model kept returning an invalid {task}: {last}")


# ──────────────────────────── premises ────────────────────────────

def suggest(n: int = 6) -> list[dict]:
    """Fresh premise ideas, steering away from ones already used."""
    used = [p["premise"] for p in history()][-30:]
    system = f"""Suggest {n} premises for long narrated stories in the niche: {cfg()['niche']}.
Each is one sentence with a specific injustice and a specific, legal kind of payback the narrator is
quietly positioned for (a certification only he holds, a contract clause, an audit trail he kept).
Avoid anything close to these earlier ones: {json.dumps(used)}
Return JSON only: {{"premises": [{{"title": "first-person YouTube title", "premise": "..."}}]}}"""
    data = extract_json(chat(system, "Give me the premises.", task="story_premises"))
    return [p for p in data.get("premises", []) if p.get("premise")][:n]


_STOP = set("a an the of to in on at for and or but with by from his her their my i he she they it is was were "
            "be been after before when who that this as into out up so than then".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOP and len(w) > 2}


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb))


def history() -> list[dict]:
    raw = get_store().read(SETTINGS, "stories.json")
    return json.loads(raw) if raw else []


def closest(premise: str, skip_slug: str | None = None) -> tuple[float, dict | None]:
    best = (0.0, None)
    for h in history():
        if h.get("slug") == skip_slug:
            continue
        s = max(similarity(premise, h["premise"]), similarity(premise, h.get("title", "")))
        if s > best[0]:
            best = (s, h)
    return best


def _remember(slug: str, premise: str, title: str) -> None:
    items = [h for h in history() if h.get("slug") != slug]
    items.append({"slug": slug, "premise": premise, "title": title,
                  "at": datetime.now(timezone.utc).isoformat()})
    get_store().write(SETTINGS, "stories.json", json.dumps(items[-500:]))


# ──────────────────────────── writing ────────────────────────────

def write(premise: str, minutes: int | None = None, notes: str | None = None, progress=None) -> Script:
    c = cfg()
    minutes = minutes or c["minutes"]
    system, user = outline_prompt(premise, minutes, notes)
    outline = _ask(system, user, Outline, "story_outline")
    per_chapter = max(200, minutes * c["words_per_minute"] // len(outline.chapters))
    chapters, so_far = [], ""
    for i, ch in enumerate(outline.chapters):
        system, user = chapter_prompt(outline, i, so_far, per_chapter)
        text = _ask(system, user, ChapterText, "story_chapter")
        beats = [Beat(narration=p.text.strip(), visual=Visual(type="broll", query=p.scene.strip()),
                      pause_after=0.35) for p in text.paragraphs if p.text.strip()]
        beats[-1].pause_after = 1.0  # a breath between chapters
        chapters.append(Chapter(title=ch.title, beats=beats))
        so_far += "\n\n".join(b.narration for b in beats) + "\n\n"
        if progress:
            progress(i + 1, len(outline.chapters))
    description = (outline.description.strip() + "\n\n" + DISCLAIMER).strip()
    script = Script(title_options=outline.title_options, thumbnail_text_options=outline.thumbnail_text_options,
                    description=description, tags=outline.tags, chapters=chapters, shorts=[])
    script.assign_ids()
    return script


def run(slug: str, premise: str | None = None, minutes: int | None = None, notes: str | None = None) -> Episode:
    ep = Episode.load(slug)
    meta = ep.read("meta.json")
    premise = (premise or meta.get("premise") or meta["topic"]).strip()
    minutes = minutes or meta.get("minutes") or cfg()["minutes"]
    print(f"[story] {premise[:80]} (~{minutes} min)")
    script = write(premise, minutes, notes,
                   progress=lambda i, n: print(f"  chapter {i}/{n}"))
    report = lint(script, None, minutes=minutes, wpm=cfg()["words_per_minute"])
    score, near = closest(premise, skip_slug=slug)
    if near and score >= cfg()["too_similar"]:
        report["issues"].append(f"Close to an earlier story ({score:.0%} overlap): \"{near.get('title')}\". "
                                "YouTube demonetizes channels that repeat themselves; change the premise.")
        report["ok"] = False
    ep.write("script.json", script.model_dump())
    ep.write("script.md", render_script(meta["topic"], script, report))
    ep.write("lint.json", report)
    _remember(slug, premise, script.title_options[0])
    meta = ep.read("meta.json")
    meta.update(premise=premise, minutes=minutes, notes=notes)
    ep.write("meta.json", meta)
    ep.set_status("scripted")
    print(f"  ✓ {report['words']} words (~{report['est_minutes']} min), {report['chapters']} chapters, {report['beats']} paragraphs")
    return ep
