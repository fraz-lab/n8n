#!/usr/bin/env python3
"""
Prepare Aluxury-style Hebrew knowledge base text for RAG / vector DB (e.g. Qdrant).

- Parses sections (A., B., ... L.) and Q&A pairs (ש: / ת:)
- Splits oversized answers into sub-chunks with overlap
- Emits JSON Lines: one object per chunk with stable id + text + payload metadata

Usage:
  python kb_to_rag_chunks.py --input kb_raw.txt --output chunks.jsonl
  python kb_to_rag_chunks.py --input kb_raw.txt --output chunks.jsonl --max-chars 900 --overlap 120

Dependencies: stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Iterator


# Section header: "A. title" at line start (Latin letter + dot)
SECTION_RE = re.compile(
    r"^[ \t]*([A-Z])\.[ \t]*(.+?)[ \t]*$",
    re.MULTILINE,
)

# Optional preamble before first section (e.g. global instructions)
PREAMBLE_SPLIT = "מאגר שאלות ותשובות מלא"

# Trailing "how to write as AI" rules (often has no ש: prefix; heading varies)
WRITER_RULES_RE = re.compile(
    r"(?s)(\*{0,2}\s*הנחיות\s+כתיבה.+)$",
)


@dataclass
class RawQA:
    section_letter: str
    section_title: str
    question: str
    answer: str
    preamble: str | None = None  # only on synthetic rows


def _normalize_ws(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _split_preamble(body: str) -> tuple[str, str]:
    """Split global intro from FAQ body when marker exists."""
    if PREAMBLE_SPLIT in body:
        idx = body.index(PREAMBLE_SPLIT)
        pre = _normalize_ws(body[:idx])
        rest = body[idx:]
        return pre, rest
    return "", body


def _find_sections(text: str) -> list[tuple[int, str, str]]:
    """
    Return list of (start_index, letter, title_line_without_letter).
    """
    matches = list(SECTION_RE.finditer(text))
    out = []
    for m in matches:
        letter = m.group(1)
        title = m.group(2).strip()
        out.append((m.start(), letter, title))
    return out


def _slice_section(text: str, start: int, end: int) -> str:
    return _normalize_ws(text[start:end])


def _parse_qa_in_block(
    section_letter: str,
    section_title: str,
    block: str,
) -> list[RawQA]:
    """
    Split block by 'ש:' markers; pair each question with following 'ת:' answer.
    Handles multiline Q/A.
    """
    block = _normalize_ws(block)
    if not block:
        return []

    # Split on ש: that starts a question (line start or after newline)
    parts = re.split(r"(?:^|\n)\s*ש\s*:\s*", block)
    out: list[RawQA] = []

    # Keep section intro / trailing prose without ש: (e.g. AI writing rules at end)
    head = parts[0].strip() if parts else ""
    if len(head) >= 80:
        out.append(
            RawQA(
                section_letter=section_letter,
                section_title=section_title,
                question=f"[{section_title}] — מבוא והקשר בפרק",
                answer=head,
            ),
        )

    for chunk in parts[1:]:
        chunk = chunk.strip()
        if not chunk:
            continue
        if "ת:" in chunk:
            q_part, rest = chunk.split("ת:", 1)
            question = _normalize_ws(q_part)
            answer = _normalize_ws(rest)
        else:
            question = _normalize_ws(chunk)
            answer = ""

        if question:
            out.append(
                RawQA(
                    section_letter=section_letter,
                    section_title=section_title,
                    question=question,
                    answer=answer,
                )
            )
    return out


def _chunk_text(
    text: str,
    max_chars: int,
    overlap: int,
) -> list[str]:
    """Character-based chunking with overlap (for long answers)."""
    text = _normalize_ws(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        piece = text[start:end]
        # Try to break at last newline within window
        if end < n:
            cut = piece.rfind("\n")
            if cut > max_chars // 2:
                piece = piece[: cut + 1].strip()
                end = start + len(piece)
        chunks.append(piece.strip())
        if end >= n:
            break
        start = max(0, end - overlap)
    return [c for c in chunks if c]


def _stable_id(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()[:24]


def parse_knowledge_document(raw: str) -> list[RawQA]:
    raw = raw.replace("\ufeff", "")
    preamble, body = _split_preamble(raw)
    body = body if body.strip() else raw

    writer_tail: str | None = None
    wm = WRITER_RULES_RE.search(body)
    if wm:
        writer_tail = _normalize_ws(wm.group(0))
        body = _normalize_ws(body[: wm.start()])

    sections = _find_sections(body)
    if not sections:
        # No A./B. headers: treat whole doc as one block
        block = _normalize_ws(body)
        qa = _parse_qa_in_block("?", "כללי", block)
        if preamble:
            qa.insert(
                0,
                RawQA(
                    section_letter="_",
                    section_title="הנחיות כלליות",
                    question="מבוא והנחיות גלובליות",
                    answer=preamble,
                    preamble="1",
                ),
            )
        return qa

    pairs: list[RawQA] = []

    if preamble:
        pairs.append(
            RawQA(
                section_letter="_",
                section_title="לפני המאגר",
                question="הנחיות למודל והקשר כללי",
                answer=preamble,
                preamble="1",
            ),
        )

    for i, (pos, letter, title) in enumerate(sections):
        start_content = body.find("\n", pos)
        if start_content == -1:
            start_content = pos
        else:
            start_content += 1

        if i + 1 < len(sections):
            end_pos = sections[i + 1][0]
        else:
            end_pos = len(body)

        section_text = _slice_section(body, start_content, end_pos)
        pairs.extend(_parse_qa_in_block(letter, title, section_text))

    if writer_tail:
        pairs.append(
            RawQA(
                section_letter="M",
                section_title="הנחיות כתיבה למודל",
                question="כללים לסגנון תשובה ומכירה (מטא-הנחיות)",
                answer=writer_tail,
            ),
        )

    return pairs


def rag_records(
    pairs: list[RawQA],
    *,
    max_chars: int,
    overlap: int,
    source: str,
) -> Iterator[dict]:
    """
    Each record:
      - id: stable hash
      - text: what you send to the embedding model (Hebrew RAG passage)
      - payload: Qdrant/metadata (filter + cite)
    """
    for qa in pairs:
        base_meta = {
            "source": source,
            "section_letter": qa.section_letter,
            "section_title": qa.section_title,
            "type": "preamble" if qa.preamble else "faq",
        }

        if not qa.answer:
            # Question-only: still embed for retrieval
            passage = f"שאלה: {qa.question}\n(אין תשובה מפורטת במאגר.)"
            rid = _stable_id(source, qa.section_letter, qa.question, "no-answer")
            yield {
                "id": rid,
                "text": passage,
                "payload": {
                    **base_meta,
                    "question": qa.question,
                    "chunk_part": 0,
                    "chunk_total": 1,
                },
            }
            continue

        answer_chunks = _chunk_text(qa.answer, max_chars=max_chars, overlap=overlap)
        total = len(answer_chunks)

        for idx, ach in enumerate(answer_chunks):
            if total == 1:
                passage = (
                    f"[{qa.section_letter}. {qa.section_title}]\n"
                    f"שאלה: {qa.question}\n"
                    f"תשובה:\n{ach}"
                )
            else:
                passage = (
                    f"[{qa.section_letter}. {qa.section_title} — חלק {idx + 1}/{total}]\n"
                    f"שאלה: {qa.question}\n"
                    f"תשובה (המשך):\n{ach}"
                )

            rid = _stable_id(
                source,
                qa.section_letter,
                qa.question,
                str(idx),
                ach[:200],
            )

            yield {
                "id": rid,
                "text": passage,
                "payload": {
                    **base_meta,
                    "question": qa.question,
                    "chunk_part": idx,
                    "chunk_total": total,
                },
            }


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert KB text to RAG JSONL chunks.")
    ap.add_argument("--input", "-i", required=True, help="UTF-8 text file (raw KB)")
    ap.add_argument("--output", "-o", required=True, help="Output .jsonl path")
    ap.add_argument(
        "--max-chars",
        type=int,
        default=1200,
        help="Max characters per answer slice before splitting (embedding window aware)",
    )
    ap.add_argument(
        "--overlap",
        type=int,
        default=150,
        help="Overlap when splitting long answers",
    )
    ap.add_argument(
        "--source",
        default="aluxury_kb_v1",
        help="Stored in payload.source for traceability",
    )
    args = ap.parse_args()

    try:
        raw = open(args.input, "r", encoding="utf-8").read()
    except OSError as e:
        print(f"Error reading input: {e}", file=sys.stderr)
        return 1

    pairs = parse_knowledge_document(raw)
    if not pairs:
        print("No Q&A pairs parsed; check file format (ש: / ת: and optional A. sections).", file=sys.stderr)
        return 1

    count = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for rec in rag_records(
            pairs,
            max_chars=args.max_chars,
            overlap=args.overlap,
            source=args.source,
        ):
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

    print(f"Wrote {count} chunks from {len(pairs)} Q&A rows -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
