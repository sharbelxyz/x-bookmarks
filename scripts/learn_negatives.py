#!/usr/bin/env python3
"""Propose exclusion rules from the decisions already made.

Every time something is marked irrelevant, that judgement is thrown away. After
785 exclusions there is a real signal sitting unused: which words show up in the
things you reject and *not* in the things you keep.

The method is discriminative, not frequency-based, and that distinction is the
whole point. "ai" is the most common word in the excluded pile — and also in the
kept pile, so it carries no information. What matters is the ratio. This uses
smoothed log-odds:

    score = ln( (excluded_hits + a) / (excluded_total + 2a) )
          - ln( (relevant_hits + a) / (relevant_total + 2a) )

A term common in both lands near zero and is correctly ignored. A term that
appears nine times in rejects and never in keeps rises to the top.

Nothing here decides anything. It emits *proposals*; a human approves each one
once, and only then does it start acting. That keeps the system's core promise
intact: rules never silently reject.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


os.umask(0o077)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "group-monitor"
PROPOSALS_PATH = DATA_DIR / "negative-proposals.json"

# Tuning. Deliberately conservative: a bad rule silently hides useful things, so
# the bar for proposing one is high.
SMOOTHING = 0.5
MIN_EXCLUDED_HITS = 5          # never generalise from a handful of examples
MAX_RELEVANT_HITS = 1          # a term that ever marked something useful is suspect
MIN_LOG_ODDS = 1.5             # ~4.5x more likely in rejects than in keeps
MIN_TERM_LENGTH = 3
MAX_PROPOSALS = 25

# Structural words carry no topical signal in either language.
STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "that", "this", "from", "are", "was",
    "have", "has", "not", "but", "all", "can", "will", "one", "out", "get", "how",
    "why", "what", "when", "who", "its", "it's", "they", "them", "their", "there",
    "here", "just", "like", "more", "most", "than", "then", "some", "any", "new",
    "now", "our", "his", "her", "she", "him", "been", "were", "would", "could",
    "should", "about", "into", "over", "after", "before", "make", "made", "many",
    "much", "very", "also", "only", "even", "still", "because", "https", "http",
    "com", "www", "org", "net", "amp", "via", "rt",
    # Arabic function words. This list has to be generous: the relevant corpus is
    # mostly English, so an ordinary Arabic word like "because" can show up zero
    # times among keeps purely by sampling and look like perfect evidence. Any
    # word that is grammar rather than subject matter must never become a rule.
    "في", "من", "على", "الى", "إلى", "عن", "مع", "هذا", "هذه", "ذلك", "التي", "الذي",
    "كان", "كانت", "يكون", "لكن", "أو", "او", "ثم", "قد", "لقد", "كل", "بعد", "قبل",
    "عند", "حتى", "اذا", "إذا", "ما", "لا", "لم", "لن", "هو", "هي", "هم", "انت", "أنت",
    "اقول", "أقول", "قال", "قلت", "تقول", "يقول", "لان", "لأن", "علشان", "عشان",
    "طيب", "يعني", "بس", "كده", "كذا", "هيك", "وانت", "وأنت", "وانا", "وأنا", "انا", "أنا",
    "احنا", "إحنا", "نحن", "انتم", "أنتم", "عنه", "عنها", "منه", "منها", "معاه", "معها",
    "له", "لها", "بيه", "فيه", "فيها", "فيك", "فيني", "عليه", "عليها", "اليه", "إليه",
    "كلها", "كله", "كلهم", "بعض", "نفس", "نفسه", "غير", "بدون", "حول", "خلال", "بين",
    "يصير", "صار", "صرت", "يصبح", "اصبح", "أصبح", "راح", "رايح", "جاي", "بدي", "ابي", "أبي",
    "شي", "شيء", "اشياء", "أشياء", "حاجة", "حاجات", "امر", "أمر", "وحدة", "واحد", "واحدة",
    "مره", "مرة", "يوم", "اليوم", "امس", "أمس", "بكرة", "دايما", "دائما", "ابدا", "أبدا",
    "مستحيل", "ممكن", "لازم", "يجب", "عندي", "عندك", "عنده", "عندها", "لدي", "لديك",
    "اكثر", "أكثر", "اقل", "أقل", "جدا", "جداً", "فقط", "ايضا", "أيضا", "كمان", "برضو",
    "كيف", "ليش", "لماذا", "متى", "وين", "اين", "أين", "مين", "منو", "ايش", "إيش", "وش",
    "انك", "أنك", "انه", "أنه", "انها", "أنها", "احد", "أحد", "الشخص", "شخص", "الناس",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{2,}|[؀-ۿ]{3,}")
# Harakat/tanween are optional in written Arabic, so the same word arrives
# spelled several ways (جدا / جدًا). Strip them so one word is one token and a
# stopword entry actually covers every spelling of it.
_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")


def normalize_arabic(term: str) -> str:
    term = _ARABIC_DIACRITICS.sub("", term)
    term = term.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    return term.replace("ة", "ه").replace("ى", "ي")


def tokenize(text: str) -> Set[str]:
    """Distinct normalized terms in a document.

    A set, not a list: a document that says "fitness" ten times is still one
    piece of evidence, and counting it ten times would manufacture confidence.
    """
    terms = set()
    for match in _WORD_RE.findall(str(text or "")):
        term = normalize_arabic(match.lower().strip(".-+#"))
        if len(term) < MIN_TERM_LENGTH or term.isdigit():
            continue
        if term in STOPWORDS or term in _NORMALIZED_STOPWORDS:
            continue
        terms.add(term)
    return terms


def _normalized_stopwords() -> Set[str]:
    """Stopwords under the same normalization tokens go through, so an entry
    written with diacritics still matches text written without them."""
    return {
        _ARABIC_DIACRITICS.sub("", w)
        .replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ة", "ه").replace("ى", "ي")
        for w in STOPWORDS
    }


_NORMALIZED_STOPWORDS = _normalized_stopwords()


def protected_terms(profile: Dict[str, Any]) -> Set[str]:
    """Terms the profile already relies on. Proposing these would be a rule that
    fights the rule next to it."""
    selection = profile.get("selection", {})
    protected = {str(t).lower() for t in selection.get("ai_terms", [])}
    for area in selection.get("project_areas", {}).values():
        protected.update(str(t).lower() for t in area.get("keywords", []))
    protected.update(str(t).lower() for t in selection.get("negative_terms", []))
    # Multi-word keywords also protect their parts: "voice agent" protects "agent".
    for term in list(protected):
        protected.update(part for part in term.split() if len(part) >= MIN_TERM_LENGTH)
    return protected


def collect_documents(conn: sqlite3.Connection) -> Tuple[List[Set[str]], List[Set[str]]]:
    """Tokenised excluded and relevant documents.

    Uses the resource's own content, not the reviewer's prose reason. The reason
    describes the decision in the model's vocabulary; the content is what the
    rule will actually have to match on later.
    """
    excluded, relevant = [], []
    for row in conn.execute(
        "SELECT status, COALESCE(title,'') || ' ' || COALESCE(content_text, source_text, '') AS body "
        "FROM resources WHERE status IN ('irrelevant','relevant')"
    ):
        terms = tokenize(row[1])
        if not terms:
            continue
        (excluded if row[0] == "irrelevant" else relevant).append(terms)
    return excluded, relevant


def propose(
    excluded_docs: List[Set[str]],
    relevant_docs: List[Set[str]],
    protected: Set[str],
    limit: int = MAX_PROPOSALS,
) -> List[Dict[str, Any]]:
    excluded_total = len(excluded_docs)
    relevant_total = len(relevant_docs)
    if excluded_total < MIN_EXCLUDED_HITS:
        return []

    excluded_counts: Counter = Counter()
    for doc in excluded_docs:
        excluded_counts.update(doc)
    relevant_counts: Counter = Counter()
    for doc in relevant_docs:
        relevant_counts.update(doc)

    proposals = []
    for term, hits in excluded_counts.items():
        if hits < MIN_EXCLUDED_HITS or term in protected:
            continue
        kept_hits = relevant_counts.get(term, 0)
        if kept_hits > MAX_RELEVANT_HITS:
            continue
        excluded_rate = (hits + SMOOTHING) / (excluded_total + 2 * SMOOTHING)
        relevant_rate = (kept_hits + SMOOTHING) / (relevant_total + 2 * SMOOTHING)
        log_odds = math.log(excluded_rate) - math.log(relevant_rate)
        if log_odds < MIN_LOG_ODDS:
            continue
        proposals.append(
            {
                "term": term,
                "excluded_hits": hits,
                "relevant_hits": kept_hits,
                "log_odds": round(log_odds, 2),
                "evidence": "appears in {} excluded item{} and {} relevant one{}".format(
                    hits, "" if hits == 1 else "s", kept_hits, "" if kept_hits == 1 else "s"
                ),
            }
        )
    proposals.sort(key=lambda p: (-p["log_odds"], -p["excluded_hits"]))
    return proposals[:limit]


def build(conn: sqlite3.Connection, profile: Dict[str, Any], limit: int = MAX_PROPOSALS) -> Dict[str, Any]:
    excluded_docs, relevant_docs = collect_documents(conn)
    proposals = propose(excluded_docs, relevant_docs, protected_terms(profile), limit)
    return {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds"),
        "excluded_documents": len(excluded_docs),
        "relevant_documents": len(relevant_docs),
        "thresholds": {
            "min_excluded_hits": MIN_EXCLUDED_HITS,
            "max_relevant_hits": MAX_RELEVANT_HITS,
            "min_log_odds": MIN_LOG_ODDS,
        },
        "proposals": proposals,
    }


def write_proposals(document: Dict[str, Any], path: Path = PROPOSALS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    path.chmod(0o600)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=MAX_PROPOSALS)
    parser.add_argument("--print", action="store_true", help="show the table instead of only writing it")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "scripts"))
    import group_monitor as monitor

    conn = monitor.connect_db()
    try:
        document = build(conn, monitor.load_profile(), args.limit)
    finally:
        conn.close()
    write_proposals(document)

    if args.print:
        print("from {} excluded and {} relevant documents:".format(
            document["excluded_documents"], document["relevant_documents"]))
        for proposal in document["proposals"]:
            print("  %-22s log-odds %5.2f  %s" % (
                proposal["term"], proposal["log_odds"], proposal["evidence"]))
    print(json.dumps({"proposals": len(document["proposals"]), "path": str(PROPOSALS_PATH)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
