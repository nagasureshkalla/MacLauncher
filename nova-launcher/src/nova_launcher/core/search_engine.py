"""In-memory application search.

Every field is lower-cased and tokenised once when the index is built, so a
keystroke only does string comparisons over a few hundred small records.

Ranking tiers (higher wins):
    exact name                        1000
    name prefix                        900
    word-in-name prefix                800
    executable / desktop-id prefix     700
    name substring                     600
    keyword / alias prefix             550
    generic-name word prefix           500
    category match                     350
    description word prefix            250
    fuzzy subsequence on name          100-300
    1-2 typo edit-distance on words    80-200
A small usage (frecency) boost breaks ties in favour of apps you actually use.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .desktop_entry import AppRecord

_WORD_RE = re.compile(r"[\w+#]+")

# A few common aliases that desktop entries rarely carry as keywords.
ALIASES: dict[str, tuple[str, ...]] = {
    "files": ("file manager", "explorer", "finder", "nautilus", "folders"),
    "terminal": ("term", "console", "shell", "cmd", "command line"),
    "settings": ("preferences", "control panel", "config"),
    "browser": ("web", "internet"),
    "editor": ("text", "code", "notepad"),
}

# Categories expanded to the words people actually type.
CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "TerminalEmulator": ("terminal", "term", "console", "shell"),
    "WebBrowser": ("browser", "web", "internet"),
    "TextEditor": ("editor", "text", "code"),
    "IDE": ("ide", "code", "editor", "development"),
    "FileManager": ("files", "file manager", "explorer", "folders"),
    "Development": ("development", "dev", "programming", "code"),
    "Settings": ("settings", "preferences"),
    "AudioVideo": ("media", "video", "audio", "music"),
    "Graphics": ("graphics", "image", "photo"),
    "Game": ("game", "games"),
    "Office": ("office", "document"),
}


def normalise(text: str) -> str:
    """Lower-case and strip accents ("Écran" -> "ecran")."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def words(text: str) -> list[str]:
    return _WORD_RE.findall(normalise(text))


@dataclass
class _Entry:
    record: AppRecord
    name: str
    name_words: list[str]
    initials: str
    ids: list[str]  # executable, desktop id stem and its dotted parts
    keywords: list[str]
    keyword_words: list[str]
    generic_words: list[str]
    category_words: list[str]
    description_words: list[str]
    name_chars: frozenset
    typo_words: list[str]


def _build_entry(r: AppRecord) -> _Entry:
    name = normalise(r.name)
    name_words = words(r.name)
    stem = normalise(r.id.removesuffix(".desktop"))
    ids = [normalise(r.executable)] if r.executable else []
    ids.append(stem)
    ids.extend(p for p in re.split(r"[._\-]", stem) if len(p) > 2)
    kws = [normalise(k) for k in r.keywords]
    cat_words: list[str] = []
    for c in r.categories:
        cat_words.append(normalise(c))
        cat_words.extend(CATEGORY_WORDS.get(c, ()))
    for key, extra in ALIASES.items():
        if key in name_words or key in cat_words:
            kws.extend(extra)
    return _Entry(
        record=r,
        name=name,
        name_words=name_words,
        initials="".join(w[0] for w in name_words if w),
        ids=list(dict.fromkeys(i for i in ids if i)),
        keywords=kws,
        keyword_words=[w for k in kws for w in k.split()],
        generic_words=words(r.generic_name),
        category_words=cat_words,
        description_words=words(r.description),
        name_chars=frozenset(name),
        typo_words=list(dict.fromkeys(name_words + [w for k in kws for w in k.split()] + ids[:2])),
    )


def fuzzy_subsequence(query: str, target: str) -> float:
    """Score 0..1 for ``query`` appearing in order inside ``target``.

    Contiguous runs and matches at word starts score higher.
    """
    if not query or len(query) > len(target):
        return 0.0
    ti = 0
    score = 0.0
    prev = -2
    for qc in query:
        idx = target.find(qc, ti)
        if idx < 0:
            return 0.0
        bonus = 1.0
        if idx == prev + 1:
            bonus += 1.0  # contiguous
        if idx == 0 or not target[idx - 1].isalnum():
            bonus += 0.8  # word start
        score += bonus
        prev = idx
        ti = idx + 1
    return min(1.0, score / (len(query) * 2.8)) * (len(query) / max(len(target), len(query) + 4)) ** 0.25


def damerau_levenshtein(a: str, b: str, limit: int) -> int:
    """Edit distance with transpositions, bailing out once ``limit`` is exceeded."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            row_min = min(row_min, v)
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _typo_limit(n: int) -> int:
    return 0 if n < 4 else (1 if n < 7 else 2)


class SearchEngine:
    def __init__(
        self,
        records: Iterable[AppRecord] = (),
        usage_score: Callable[[str], float] | None = None,
        fuzzy: bool = True,
        descriptions: bool = True,
    ):
        self.fuzzy = fuzzy
        self.descriptions = descriptions
        self._usage = usage_score or (lambda _id: 0.0)
        self._entries: list[_Entry] = []
        self.rebuild(records)

    def rebuild(self, records: Iterable[AppRecord]) -> None:
        self._entries = [_build_entry(r) for r in records]

    def __len__(self) -> int:
        return len(self._entries)

    def _score_term(self, e: _Entry, t: str) -> float:
        if e.name == t:
            return 1000
        if e.name.startswith(t):
            return 900
        if any(w.startswith(t) for w in e.name_words):
            return 800
        if any(i.startswith(t) for i in e.ids):
            return 700
        if t in e.name:
            return 600
        if len(t) >= 2 and e.initials.startswith(t) and len(e.initials) > 1:
            return 580  # "vsc" -> Visual Studio Code
        if any(k.startswith(t) for k in e.keywords) or any(k.startswith(t) for k in e.keyword_words):
            return 550
        if any(w.startswith(t) for w in e.generic_words):
            return 500
        if any(c.startswith(t) for c in e.category_words):
            return 350
        if self.descriptions and len(t) >= 3 and any(w.startswith(t) for w in e.description_words):
            return 250
        if not self.fuzzy:
            return 0
        best = 0.0
        # Cheap rejection first: a subsequence needs every query character.
        if len(t) >= 2 and set(t) <= e.name_chars:
            f = fuzzy_subsequence(t, e.name)
            if f > 0.35:
                best = 100 + 200 * f
        limit = _typo_limit(len(t))
        if limit:
            for w in e.typo_words:
                # Typos rarely hit the first letter; also allow a swapped first pair.
                if not w or (w[0] != t[0] and not (len(w) > 1 and w[1] == t[0] and w[0] == t[1])):
                    continue
                # Compare against the word, or its prefix of similar length so
                # "firefx" still matches "firefox" while typing.
                cand = w[: len(t) + 1] if len(w) > len(t) + 1 else w
                d = damerau_levenshtein(t, cand, limit)
                if d <= limit:
                    best = max(best, 200 - 60 * d)
        return best

    def search(self, query: str, limit: int = 20) -> list[tuple[float, AppRecord]]:
        terms = words(query)
        if not terms:
            return []
        results: list[tuple[float, AppRecord]] = []
        for e in self._entries:
            total = 0.0
            for t in terms:
                s = self._score_term(e, t)
                if s <= 0:
                    total = 0.0
                    break
                total += s
            if total <= 0:
                continue
            total = total / len(terms)
            # Usage boost: up to +80, enough to reorder within a tier only.
            total += min(80.0, self._usage(e.record.id) * 8.0)
            results.append((total, e.record))
        results.sort(key=lambda x: (-x[0], x[1].name.casefold()))
        return results[:limit]

    def ids(self) -> Sequence[str]:
        return [e.record.id for e in self._entries]
