"""Stage 3: documentary script.

fact sheet -> script.json (chapters of beats, each beat = narration + the shot to cut to)
           -> script.md   (what you read and edit)
           -> lint report (AI phrases, length, numbers not backed by the fact sheet)

script.json is what the voice, visuals and Shorts stages read later.
"""
from __future__ import annotations

import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import load_config, load_prompt
from .episode import Episode
from .llm import LLMError, chat, extract_json

VisualType = Literal["archive_photo", "broll", "document", "chart", "map", "title_card"]


class Visual(BaseModel):
    type: VisualType
    query: str
    note: Optional[str] = None


class Beat(BaseModel):
    id: str = ""
    narration: str
    visual: Visual
    on_screen_text: Optional[str] = None
    pause_after: float = Field(default=0.3, ge=0, le=3)


class Chapter(BaseModel):
    title: str
    beats: list[Beat] = Field(min_length=1)


class ShortPick(BaseModel):
    chapter: int
    beats: list[int] = Field(min_length=2, max_length=2)
    hook: str
    why: str = ""


class Script(BaseModel):
    title_options: list[str] = Field(min_length=1)
    thumbnail_text_options: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    chapters: list[Chapter] = Field(min_length=2)
    shorts: list[ShortPick] = Field(default_factory=list)

    @field_validator("shorts")
    @classmethod
    def _ranges_ok(cls, shorts, info):
        chapters = info.data.get("chapters") or []
        for s in shorts:
            if not 0 <= s.chapter < len(chapters):
                raise ValueError(f"short points at chapter {s.chapter}, but there are {len(chapters)} chapters (0-based)")
            start, end = s.beats
            n = len(chapters[s.chapter].beats)
            if not 1 <= start <= end <= n:
                raise ValueError(f"short beat range {s.beats} is outside chapter {s.chapter} (has {n} beats)")
        return shorts

    def assign_ids(self) -> None:
        # Stable ids like c2b05 so later stages can name audio/image files per beat.
        for ci, ch in enumerate(self.chapters):
            for bi, beat in enumerate(ch.beats, 1):
                beat.id = f"c{ci}b{bi:02d}"

    def word_count(self) -> int:
        return sum(len(b.narration.split()) for ch in self.chapters for b in ch.beats)


# ──────────────────────────── prompt ────────────────────────────

def build_system_prompt() -> str:
    cfg = load_config()
    s = cfg["script"]
    target_words = s["long_minutes"] * s["words_per_minute"]
    fills = {
        "{voice_style}": cfg["channel"]["voice_style"].strip(),
        "{banned_phrases}": ", ".join(f'"{p}"' for p in cfg["banned_phrases"]),
        "{min_chapters}": str(s["min_chapters"]),
        "{max_chapters}": str(s["max_chapters"]),
        "{target_words}": str(target_words),
        "{target_minutes}": str(s["long_minutes"]),
        "{shorts_count}": str(s["shorts_per_video"]),
    }
    prompt = load_prompt("script_system.md")
    for k, v in fills.items():  # plain replace: the prompt also contains JSON braces
        prompt = prompt.replace(k, v)
    return prompt


def build_user_prompt(topic: str, factsheet: dict, angle: str | None, notes: str | None) -> str:
    fs = {k: v for k, v in factsheet.items() if k not in ("external_links",)}
    parts = [f"TOPIC: {topic}"]
    if angle:
        parts.append(f"STORY ANGLE (chosen by the creator, follow it): {angle}")
    if notes:
        parts.append(f"CREATOR NOTES (their own take, weave it in): {notes}")
    parts.append("FACT SHEET:\n" + json.dumps(fs, indent=2, ensure_ascii=False))
    return "\n\n".join(parts)


# ──────────────────────────── lint ────────────────────────────

_NUMBER = re.compile(r"\b(?:1[89]\d{2}|20\d{2})\b|\b\d+(?:[.,]\d+)?\s*(?:%|percent|million|billion|trillion)\b", re.I)


def lint(script: Script, factsheet: dict) -> dict:
    cfg = load_config()
    s = cfg["script"]
    target = s["long_minutes"] * s["words_per_minute"]
    words = script.word_count()
    fs_text = json.dumps(factsheet, ensure_ascii=False).lower()

    banned_hits, unbacked = [], []
    for ch in script.chapters:
        for b in ch.beats:
            low = b.narration.lower()
            banned_hits += [(b.id, p) for p in cfg["banned_phrases"] if p in low]
            # Every year/figure in the narration should exist somewhere in the fact sheet.
            for m in _NUMBER.finditer(b.narration):
                core = re.match(r"[\d.,]+", m.group(0)).group(0).rstrip(".,")
                if core.lower() not in fs_text:
                    unbacked.append((b.id, m.group(0)))

    issues = []
    if banned_hits:
        issues.append("Banned phrases: " + ", ".join(f"{i} '{p}'" for i, p in banned_hits))
    if unbacked:
        issues.append("Numbers not found in fact sheet (possible invention): "
                      + ", ".join(f"{i} '{n}'" for i, n in unbacked))
    ratio = words / target
    if not 0.8 <= ratio <= 1.2:
        issues.append(f"Length off: {words} words vs ~{target} target ({ratio:.0%})")

    return {
        "words": words,
        "est_minutes": round(words / s["words_per_minute"], 1),
        "chapters": len(script.chapters),
        "beats": sum(len(c.beats) for c in script.chapters),
        "shorts": len(script.shorts),
        "issues": issues,
        "ok": not banned_hits and not unbacked,
    }


# ──────────────────────────── run ────────────────────────────

def generate(topic: str, factsheet: dict, angle: str | None = None, notes: str | None = None) -> Script:
    system = build_system_prompt()
    user = build_user_prompt(topic, factsheet, angle, notes)
    last_error = None
    for attempt in range(2):
        prompt = user if attempt == 0 else (
            user + f"\n\nYOUR PREVIOUS REPLY WAS INVALID: {last_error}\nReturn the full corrected JSON.")
        reply = chat(system, prompt, task="script")
        try:
            script = Script.model_validate(extract_json(reply))
            script.assign_ids()
            return script
        except (ValidationError, LLMError) as e:
            last_error = str(e)[:1500]
            print(f"  ! script failed validation (attempt {attempt + 1}), asking the model to fix it")
    raise LLMError(f"Script still invalid after retry: {last_error}")


def run(slug: str, angle: str | None = None, notes: str | None = None) -> Episode:
    ep = Episode.load(slug)
    factsheet = ep.read("factsheet.json")
    if not factsheet:
        raise FileNotFoundError("No factsheet.json yet. Run `research` first.")
    topic = ep.read("meta.json")["topic"]
    print(f"[script] {topic}")

    script = generate(topic, factsheet, angle, notes)
    report = lint(script, factsheet)

    ep.write("script.json", script.model_dump())
    ep.write("script.md", render_script(topic, script, report))
    ep.write("lint.json", report)
    ep.set_status("scripted")

    print(f"  ✓ {report['words']} words (~{report['est_minutes']} min), "
          f"{report['chapters']} chapters, {report['beats']} beats, {report['shorts']} Shorts flagged")
    for issue in report["issues"]:
        print(f"  ⚠ {issue}")
    return ep


def render_script(topic: str, script: Script, report: dict) -> str:
    out = [f"# {topic}", ""]
    out += ["## Lint", f"- {report['words']} words, ~{report['est_minutes']} min, "
                       f"{report['beats']} beats, {report['shorts']} Shorts"]
    out += [f"- ⚠️ {i}" for i in report["issues"]] or ["- ✅ clean"]
    out += ["", "## Title options", *[f"- {t}" for t in script.title_options]]
    out += ["", "## Thumbnail text", *[f"- {t}" for t in script.thumbnail_text_options], ""]

    for ci, ch in enumerate(script.chapters):
        out += [f"## {ci}. {ch.title}", ""]
        for b in ch.beats:
            out.append(f"**[{b.id}]** {b.narration}")
            meta = f"`{b.visual.type}: {b.visual.query}`"
            if b.on_screen_text:
                meta += f" · lower-third: _{b.on_screen_text}_"
            if b.pause_after >= 0.8:
                meta += f" · pause {b.pause_after}s"
            out += [f"> {meta}", ""]

    if script.shorts:
        out += ["## Shorts to cut", ""]
        for s in script.shorts:
            start, end = s.beats
            out.append(f"- **\"{s.hook}\"** — chapter {s.chapter}, beats {start}–{end}. {s.why}")
    out += ["", "## Description", script.description, "", "**Tags:** " + ", ".join(script.tags)]
    return "\n".join(out)
