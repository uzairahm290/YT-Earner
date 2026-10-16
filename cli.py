#!/usr/bin/env python3
"""Command line for the documentary pipeline.

  python cli.py topics                         # shortlist of story ideas
  python cli.py research "Nokia"               # fact sheet -> episodes/nokia/factsheet.md
  python cli.py script nokia --angle "..."     # documentary script -> episodes/nokia/script.md
  python cli.py make "Nokia" --angle "..."     # research + script in one go
  python cli.py list                           # all episodes and where they're at

Add --offline to topics/research/make to run on test fixtures (no internet, no API key needed
when combined with DOCU_MOCK_LLM=1).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from pipeline import research, script, topics
from pipeline.config import EPISODES_DIR, ROOT
from pipeline.episode import Episode
from pipeline.llm import LLMError


def cmd_topics(a):
    found = topics.find_topics(offline=a.offline)
    md = topics.format_topics(found)
    print(md)
    out = ROOT / "episodes" / f"_topics-{date.today().isoformat()}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"\nSaved to {out.relative_to(ROOT)}")


def cmd_research(a):
    ep = research.run(a.topic, extra=a.extra, offline=a.offline)
    print(f"\nNext: read episodes/{ep.slug}/factsheet.md, tick the ⚠️ items, then:\n"
          f"  python cli.py script {ep.slug} --angle \"<pick one of the story angles>\"")


def cmd_script(a):
    ep = script.run(a.slug, angle=a.angle, notes=a.notes)
    print(f"\nNext: read and edit episodes/{ep.slug}/script.md")


def cmd_make(a):
    ep = research.run(a.topic, extra=a.extra, offline=a.offline)
    script.run(ep.slug, angle=a.angle, notes=a.notes)
    print(f"\nDone. Review episodes/{ep.slug}/factsheet.md first, then script.md")


def cmd_list(_):
    eps = sorted(p for p in EPISODES_DIR.iterdir() if p.is_dir()) if EPISODES_DIR.exists() else []
    if not eps:
        print("No episodes yet.")
    for p in eps:
        meta = Episode(p.name).read("meta.json") or {}
        print(f"  {p.name:<40} {meta.get('status', '?'):<12} {meta.get('topic', '')}")


def main():
    ap = argparse.ArgumentParser(description="Documentary pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("topics", help="find topic candidates")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(fn=cmd_topics)

    for name, fn in (("research", cmd_research), ("make", cmd_make)):
        p = sub.add_parser(name)
        p.add_argument("topic", help='e.g. "Nokia" or "Theranos"')
        p.add_argument("--extra", nargs="*", default=[],
                       help='extra Wikipedia articles, e.g. --extra "Stephen Elop" "Symbian"')
        p.add_argument("--offline", action="store_true")
        if name == "make":
            p.add_argument("--angle")
            p.add_argument("--notes")
        p.set_defaults(fn=fn)

    p = sub.add_parser("script", help="write the documentary script for an episode")
    p.add_argument("slug")
    p.add_argument("--angle", help="the story angle to follow (pick from factsheet.md)")
    p.add_argument("--notes", help="your own take, worked into the narration")
    p.set_defaults(fn=cmd_script)

    p = sub.add_parser("list", help="show episodes")
    p.set_defaults(fn=cmd_list)

    a = ap.parse_args()
    try:
        a.fn(a)
    except (LLMError, FileNotFoundError, ValueError) as e:
        print(f"\n✗ {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
