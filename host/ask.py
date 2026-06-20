"""ask.py — ask questions of your captured notes, from the command line.

The answer is grounded in the Cognee knowledge graph built from your captures
and synthesized by the local Exo LLM. Fully offline.

    uv run python host/ask.py "what did I decide about the microphone?"
    uv run python host/ask.py            # then type questions, one per line

Exit the interactive prompt with Ctrl-D or an empty line.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain


def answer(question: str) -> None:
    question = question.strip()
    if not question:
        return
    print(brain.ask(question) or "(no answer — is anything captured yet?)")


def main() -> None:
    if len(sys.argv) > 1:  # one-shot: everything after argv[0] is the question
        answer(" ".join(sys.argv[1:]))
        return
    # interactive: read questions until EOF / blank line
    try:
        while True:
            q = input("ask> ")
            if not q.strip():
                break
            answer(q)
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
