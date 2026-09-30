"""Word segmentation for text that has no spaces in it.

A latin query splits on whitespace and the index is already right. Chinese
writes without spaces, so "the Nanjing Yangtze River Bridge" is one run of
characters, and an index that cuts it per character matches every document
containing any of them -- which is how a keyword search over Chinese answers
with noise rather than with answers.

This segments against a dictionary instead: a trie of words with their corpus
frequencies, searched forward and backward, with the ambiguous spans resolved
by scoring whole candidate segmentations rather than by taking the first fit.

Ported from Infinity's ``rag_tokenizer`` (Apache-2.0; see NOTICES.md), which is
the tokenizer RAGFlow indexes with. Kept as a port rather than a dependency
because the package it lives in is the Infinity vector database's client, and a
knowledge base that segments Chinese should not need a second vector store
installed to do it.

The dictionary is not in the repository: it is 8 MB, and it is fetched into
``res/`` at install time. See :func:`dictionary_path`.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import string
import unicodedata
from pathlib import Path

import datrie  # ty: ignore[unresolved-import]  # a C extension with no stubs
from hanziconv import HanziConv
from loguru import logger
from nltk import word_tokenize
from nltk.stem import SnowballStemmer, WordNetLemmatizer

# Map language names (lowercase) to NLTK SnowballStemmer language names.
# Used by set_language() to configure language-specific stemming.
_SNOWBALL_LANGUAGE_MAP = {
    "english": "english",
    "dutch": "dutch",
    "german": "german",
    "french": "french",
    "spanish": "spanish",
    "italian": "italian",
    "portuguese": "portuguese",
    "portuguese br": "portuguese",
    "russian": "russian",
    "arabic": "arabic",
    "danish": "danish",
    "finnish": "finnish",
    "hungarian": "hungarian",
    "norwegian": "norwegian",
    "romanian": "romanian",
    "swedish": "swedish",
    "turkish": "turkish",
}

# Languages tokenized with diacritics folded to ASCII and no stemming.
# SPLIT_CHAR only keeps ASCII letter runs whole, so accented words would
# otherwise be fragmented before indexing ('škola' -> 'š kola'), and
# neither language has a Snowball stemmer.
_DIACRITIC_FOLDING_LANGUAGES = {"slovak", "czech"}

#: The punctuation a run of text is cut on, as one character class. The
#: full-width half is written as escapes rather than as itself: `raven/core/`
#: is not one of the CJK exemption zones in AGENTS.md section 1.3, and a raw
#: string would carry the backslashes through instead of the characters.
_FULL_WIDTH_PUNCTUATION = (
    "\u300a\u300b\uff0c\u3002\uff1f\u3001\uff1b\u2018\u2019\uff1a"
    "\u201c\u201d\u3010\u3011\uff01\uffe5\u2026\uff08\uff09\u2014"
)
_SPLIT_CHAR = r"([ ,\.<>/?;:'\[\]\\`!@#$%^&*\(\)\{\}\|_+=" + _FULL_WIDTH_PUNCTUATION + r"~%-]+|[a-zA-Z0-9,\.-]+)"


#: Where the dictionary is fetched to. Beside this module rather than under the
#: data directory because it is part of the install rather than part of a
#: deployment's state: two Ravens on one machine share it, and reinstalling is
#: what replaces it.
RES_DIR = Path(__file__).resolve().parent / "res"

DICTIONARY = "huqie.txt"
"""The word list, as Infinity's resource repository names it (MIT; see
NOTICES.md). Roughly 8 MB, which is why it is downloaded rather than tracked --
see ``scripts/fetch_resources.py``."""


class DictionaryMissingError(RuntimeError):
    """The dictionary has not been fetched, so nothing can be segmented."""


def dictionary_path(*, required: bool = False) -> Path:
    """Where the dictionary is, whether or not it is there yet.

    ``required`` turns its absence into a raise carrying the one command that
    fixes it. Without that the failure is a stack trace inside a trie library,
    which says nothing about the install step that was skipped.
    """
    path = RES_DIR / DICTIONARY
    if required and not path.is_file():
        raise DictionaryMissingError(
            f"the word dictionary is not installed at {path}; run `make fetch-resources` "
            "(or `uv run python scripts/fetch_resources.py`) to download it"
        )
    return path


def available() -> bool:
    """Whether segmentation can run at all. Asked before it is needed."""
    return dictionary_path().is_file()


def _use_bundled_corpora() -> None:
    """Let nltk see the corpora fetched into this package.

    Prepended to its search path rather than exported as ``NLTK_DATA``: the
    variable is process-wide and a host that already sets it means something by
    it, while a path added here is scoped to the one library that reads it. The
    host's own locations stay behind ours, so a corpus installed system-wide is
    still found when the install-time download did not run.
    """
    import nltk

    bundled = str(RES_DIR / "nltk_data")
    if bundled not in nltk.data.path:
        nltk.data.path.insert(0, bundled)


_use_bundled_corpora()


def _fold_char(char: str) -> str:
    # Latin-1 Supplement and Latin Extended-A letters whose NFD decomposition
    # is one ASCII letter plus combining marks fold to that letter (Š -> S,
    # ď -> d); everything else (æ, ø, ł, ß, non-Latin scripts) is kept. Same
    # rule as RAGAnalyzer::FoldDiacritics in the C++ analyzer.
    if not (0xC0 <= ord(char) < 0x180):
        return char
    decomposed = unicodedata.normalize("NFD", char)
    base = decomposed[0]
    if base.isascii() and base.isalpha() and all(unicodedata.combining(c) for c in decomposed[1:]):
        return base
    return char


def fold_diacritics(text: str) -> str:
    """Fold Latin diacritics to ASCII: 'škola' -> 'skola'."""
    if text.isascii():
        return text
    return "".join(_fold_char(c) for c in text)


class Tokenizer:
    def key_(self, line):
        return str(line.lower().encode("utf-8"))[2:-1]

    def rkey_(self, line):
        return str(("DD" + (line[::-1].lower())).encode("utf-8"))[2:-1]

    def _load_dict(self, fnm):
        logging.info(f"[HUQIE]:Build trie from {fnm}")
        try:
            of = open(fnm, "r", encoding="utf-8")
            while True:
                line = of.readline()
                if not line:
                    break
                line = re.sub(r"[\r\n]+", "", line)
                line = re.split(r"[ \t]", line)
                k = self.key_(line[0])
                F = int(math.log(float(line[1]) / self.DENOMINATOR) + 0.5)
                if k not in self.trie_ or self.trie_[k][0] < F:
                    self.trie_[self.key_(line[0])] = (F, line[2])
                self.trie_[self.rkey_(line[0])] = 1

            dict_file_cache = fnm + ".trie"
            logging.info(f"[HUQIE]:Build trie cache to {dict_file_cache}")
            self.trie_.save(dict_file_cache)
            of.close()
        except Exception:
            logging.exception(f"[HUQIE]:Build trie {fnm} failed")

    def __init__(self, debug=False, user_dict=None):
        self.DEBUG = debug
        self.DENOMINATOR = 1000000

        if user_dict and os.path.exists(user_dict):
            self.DIR_ = user_dict
        else:
            if user_dict:
                logger.warning("tokenizer: no dictionary at {}; using the shipped one", user_dict)
            # Raised rather than `exit(1)`, which is what the original does: a
            # library that calls exit takes the gateway down with it, and the
            # caller here can perfectly well fall back to not segmenting.
            self.DIR_ = str(dictionary_path(required=True))

        self.stemmer = SnowballStemmer("english")
        self.lemmatizer = WordNetLemmatizer()
        self._use_lemmatizer = True  # WordNet only supports English
        self._fold_diacritics = False

        self.SPLIT_CHAR = _SPLIT_CHAR

        trie_file_name = self.DIR_ + ".trie"
        # check if trie file existence
        if os.path.exists(trie_file_name):
            try:
                # load trie from file
                self.trie_ = datrie.Trie.load(trie_file_name)
                return
            except Exception:
                # fail to load trie from file, build default trie
                logging.exception(f"[HUQIE]:Fail to load trie file {trie_file_name}, build the default trie file")
                self.trie_ = datrie.Trie(string.printable)
        else:
            # file not exist, build default trie
            logging.info(f"[HUQIE]:Trie file {trie_file_name} not found, build the default trie file")
            self.trie_ = datrie.Trie(string.printable)

        # load data from dict file and save to trie file
        self._load_dict(self.DIR_)

    def load_user_dict(self, fnm):
        try:
            self.trie_ = datrie.Trie.load(fnm + ".trie")
            return
        except Exception:
            self.trie_ = datrie.Trie(string.printable)
        self._load_dict(fnm)

    def add_user_dict(self, fnm):
        self._load_dict(fnm)

    def set_language(self, language: str):
        """Configure stemmer/lemmatizer for the given language.

        Args:
            language: Language name (e.g. "English", "Dutch", "Chinese").
                      Case-insensitive.
        """
        lang_key = language.strip().lower()

        self._fold_diacritics = lang_key in _DIACRITIC_FOLDING_LANGUAGES
        if self._fold_diacritics:
            # Folded to ASCII in tokenize() and left unstemmed: there is no
            # Snowball stemmer for these languages (see _normalize_token).
            logging.debug("Tokenizer language set to '%s' (diacritics folding, no stemming)", language)
            return

        snowball_lang = _SNOWBALL_LANGUAGE_MAP.get(lang_key)

        if snowball_lang is not None:
            self.stemmer = SnowballStemmer(snowball_lang)
            if snowball_lang == "english":
                self.lemmatizer = WordNetLemmatizer()
                self._use_lemmatizer = True
            else:
                # WordNet only supports English; disable lemmatizer for
                # other languages and rely on Snowball stemming alone.
                self._use_lemmatizer = False
            logging.debug(
                "Tokenizer language set to '%s' (Snowball: %s, lemmatizer: %s)",
                language,
                snowball_lang,
                self._use_lemmatizer,
            )
        else:
            # Unsupported language (Chinese, Japanese, Korean, etc.) –
            # keep defaults.  CJK text uses dictionary segmentation,
            # not stemming.
            logging.debug(
                "Language '%s' has no Snowball stemmer; keeping defaults",
                language,
            )

    def _strQ2B(self, ustring):
        """Convert full-width characters to half-width characters"""
        rstring = ""
        for uchar in ustring:
            inside_code = ord(uchar)
            if inside_code == 0x3000:
                inside_code = 0x0020
            else:
                inside_code -= 0xFEE0
            if (
                inside_code < 0x0020 or inside_code > 0x7E
            ):  # After the conversion, if it's not a half-width character, return the original character.
                rstring += uchar
            else:
                rstring += chr(inside_code)
        return rstring

    def _tradi2simp(self, line):
        return HanziConv.toSimplified(line)

    def dfs_(self, chars, s, preTks, tkslist, _depth=0, _memo=None):
        if _memo is None:
            _memo = {}
        MAX_DEPTH = 10
        if _depth > MAX_DEPTH:
            if s < len(chars):
                copy_pretks = copy.deepcopy(preTks)
                remaining = "".join(chars[s:])
                copy_pretks.append((remaining, (-12, "")))
                tkslist.append(copy_pretks)
            return s

        state_key = (s, tuple(tk[0] for tk in preTks)) if preTks else (s, None)
        if state_key in _memo:
            return _memo[state_key]

        res = s
        if s >= len(chars):
            tkslist.append(preTks)
            _memo[state_key] = s
            return s
        if s < len(chars) - 4:
            is_repetitive = True
            char_to_check = chars[s]
            for i in range(1, 5):
                if s + i >= len(chars) or chars[s + i] != char_to_check:
                    is_repetitive = False
                    break
            if is_repetitive:
                end = s
                while end < len(chars) and chars[end] == char_to_check:
                    end += 1
                mid = s + min(10, end - s)
                t = "".join(chars[s:mid])
                k = self.key_(t)
                copy_pretks = copy.deepcopy(preTks)
                if k in self.trie_:
                    copy_pretks.append((t, self.trie_[k]))
                else:
                    copy_pretks.append((t, (-12, "")))
                next_res = self.dfs_(chars, mid, copy_pretks, tkslist, _depth + 1, _memo)
                res = max(res, next_res)
                _memo[state_key] = res
                return res

        S = s + 1
        if s + 2 <= len(chars):
            t1 = "".join(chars[s : s + 1])
            t2 = "".join(chars[s : s + 2])
            if self.trie_.has_keys_with_prefix(self.key_(t1)) and not self.trie_.has_keys_with_prefix(self.key_(t2)):
                S = s + 2
        if len(preTks) > 2 and len(preTks[-1][0]) == 1 and len(preTks[-2][0]) == 1 and len(preTks[-3][0]) == 1:
            t1 = preTks[-1][0] + "".join(chars[s : s + 1])
            if self.trie_.has_keys_with_prefix(self.key_(t1)):
                S = s + 2

        for e in range(S, len(chars) + 1):
            t = "".join(chars[s:e])
            k = self.key_(t)
            if e > s + 1 and not self.trie_.has_keys_with_prefix(k):
                break
            if k in self.trie_:
                pretks = copy.deepcopy(preTks)
                pretks.append((t, self.trie_[k]))
                res = max(res, self.dfs_(chars, e, pretks, tkslist, _depth + 1, _memo))

        if res > s:
            _memo[state_key] = res
            return res

        t = "".join(chars[s : s + 1])
        k = self.key_(t)
        copy_pretks = copy.deepcopy(preTks)
        if k in self.trie_:
            copy_pretks.append((t, self.trie_[k]))
        else:
            copy_pretks.append((t, (-12, "")))
        result = self.dfs_(chars, s + 1, copy_pretks, tkslist, _depth + 1, _memo)
        _memo[state_key] = result
        return result

    def freq(self, tk):
        k = self.key_(tk)
        if k not in self.trie_:
            return 0
        return int(math.exp(self.trie_[k][0]) * self.DENOMINATOR + 0.5)

    def tag(self, tk):
        k = self.key_(tk)
        if k not in self.trie_:
            return ""
        return self.trie_[k][1]

    def score_(self, tfts):
        B = 30
        F, L, tks = 0, 0, []
        for tk, (freq, tag) in tfts:
            F += freq
            L += 0 if len(tk) < 2 else 1
            tks.append(tk)
        # F /= len(tks)
        L /= len(tks)
        logging.debug(f"[SC] {tks} {len(tks)} {L} {F} {B / len(tks) + L + F}")
        return tks, B / len(tks) + L + F

    def _sort_tokens(self, tkslist):
        res = []
        for tfts in tkslist:
            tks, s = self.score_(tfts)
            res.append((tks, s))
        return sorted(res, key=lambda x: x[1], reverse=True)

    def merge_(self, tks):
        # if split chars is part of token
        res = []
        tks = re.sub(r"[ ]+", " ", tks).split()
        s = 0
        while True:
            if s >= len(tks):
                break
            E = s + 1
            for e in range(s + 2, min(len(tks) + 2, s + 6)):
                tk = "".join(tks[s:e])
                if re.search(self.SPLIT_CHAR, tk) and self.freq(tk):
                    E = e
            res.append("".join(tks[s:E]))
            s = E

        return " ".join(res)

    def _max_forward(self, line):
        res = []
        s = 0
        while s < len(line):
            e = s + 1
            t = line[s:e]
            while e < len(line) and self.trie_.has_keys_with_prefix(self.key_(t)):
                e += 1
                t = line[s:e]

            while e - 1 > s and self.key_(t) not in self.trie_:
                e -= 1
                t = line[s:e]

            if self.key_(t) in self.trie_:
                res.append((t, self.trie_[self.key_(t)]))
            else:
                res.append((t, (0, "")))

            s = e

        return self.score_(res)

    def _max_backward(self, line):
        res = []
        s = len(line) - 1
        while s >= 0:
            e = s + 1
            t = line[s:e]
            while s > 0 and self.trie_.has_keys_with_prefix(self.rkey_(t)):
                s -= 1
                t = line[s:e]

            while s + 1 < e and self.key_(t) not in self.trie_:
                s += 1
                t = line[s:e]

            if self.key_(t) in self.trie_:
                res.append((t, self.trie_[self.key_(t)]))
            else:
                res.append((t, (0, "")))

            s -= 1

        return self.score_(res[::-1])

    def english_normalize_(self, tks):
        return [self._normalize_token(t) for t in tks]

    def _normalize_token(self, t: str) -> str:
        """Stem (and optionally lemmatize) a single alphabetic token.

        When the lemmatizer is enabled (English), applies lemmatization
        before stemming.  For other Snowball-supported languages, only
        stemming is applied.  Non-alphabetic tokens are returned as-is,
        and so is every token of a diacritic-folding language.
        """
        if self._fold_diacritics:
            return t
        if re.match(r"[a-zA-Z_-]+$", t):
            # `SnowballStemmer(...)` is a factory returning one of a union of
            # language classes, so the checker reads `.stem` as unbound and
            # counts the argument as the missing `self`. Both calls pass a
            # token.
            if self._use_lemmatizer:
                return self.stemmer.stem(self.lemmatizer.lemmatize(t))  # ty: ignore[missing-argument]
            return self.stemmer.stem(t)  # ty: ignore[missing-argument]
        return t

    def _split_by_lang(self, line):
        txt_lang_pairs = []
        arr = re.split(self.SPLIT_CHAR, line)
        for a in arr:
            if not a:
                continue
            s = 0
            e = s + 1
            zh = is_chinese(a[s])
            while e < len(a):
                _zh = is_chinese(a[e])
                if _zh == zh:
                    e += 1
                    continue
                txt_lang_pairs.append((a[s:e], zh))
                s = e
                e = s + 1
                zh = _zh
            if s >= len(a):
                continue
            txt_lang_pairs.append((a[s:e], zh))
        return txt_lang_pairs

    def tokenize(self, line: str) -> str:
        if self._fold_diacritics:
            line = fold_diacritics(line)
        line = re.sub(r"\W+", " ", line)
        line = self._strQ2B(line).lower()
        line = self._tradi2simp(line)

        arr = self._split_by_lang(line)
        res = []
        for L, lang in arr:
            if not lang:
                res.extend([self._normalize_token(t) for t in word_tokenize(L)])
                continue
            if len(L) < 2 or re.match(r"[a-z\.-]+$", L) or re.match(r"[0-9\.-]+$", L):
                res.append(L)
                continue

            # use maxforward for the first time
            tks, s = self._max_forward(L)
            tks1, s1 = self._max_backward(L)
            if self.DEBUG:
                logging.debug(f"[FW] {tks} {s}")
                logging.debug(f"[BW] {tks1} {s1}")

            i, j, _i, _j = 0, 0, 0, 0
            same = 0
            while i + same < len(tks1) and j + same < len(tks) and tks1[i + same] == tks[j + same]:
                same += 1
            if same > 0:
                res.append(" ".join(tks[j : j + same]))
            _i = i + same
            _j = j + same
            j = _j + 1
            i = _i + 1

            while i < len(tks1) and j < len(tks):
                tk1, tk = "".join(tks1[_i:i]), "".join(tks[_j:j])
                if tk1 != tk:
                    if len(tk1) > len(tk):
                        j += 1
                    else:
                        i += 1
                    continue

                if tks1[i] != tks[j]:
                    i += 1
                    j += 1
                    continue
                # backward tokens from_i to i are different from forward tokens from _j to j.
                tkslist = []
                self.dfs_("".join(tks[_j:j]), 0, [], tkslist)
                res.append(" ".join(self._sort_tokens(tkslist)[0][0]))

                same = 1
                while i + same < len(tks1) and j + same < len(tks) and tks1[i + same] == tks[j + same]:
                    same += 1
                res.append(" ".join(tks[j : j + same]))
                _i = i + same
                _j = j + same
                j = _j + 1
                i = _i + 1

            if _i < len(tks1):
                assert _j < len(tks)
                assert "".join(tks1[_i:]) == "".join(tks[_j:])
                tkslist = []
                self.dfs_("".join(tks[_j:]), 0, [], tkslist)
                res.append(" ".join(self._sort_tokens(tkslist)[0][0]))

        res = " ".join(res)
        logging.debug(f"[TKS] {self.merge_(res)}")
        return self.merge_(res)

    def fine_grained_tokenize(self, tks: str) -> str:
        tks = tks.split()
        zh_num = len([1 for c in tks if c and is_chinese(c[0])])
        if zh_num < len(tks) * 0.2:
            res = []
            for tk in tks:
                res.extend(tk.split("/"))
            return " ".join(res)

        res = []
        for tk in tks:
            if len(tk) < 3 or re.match(r"[0-9,\.-]+$", tk):
                res.append(tk)
                continue
            tkslist = []
            if len(tk) > 10:
                tkslist.append(tk)
            else:
                self.dfs_(tk, 0, [], tkslist)
            if len(tkslist) < 2:
                res.append(tk)
                continue
            stk = self._sort_tokens(tkslist)[1][0]
            if len(stk) == len(tk):
                stk = tk
            else:
                if re.match(r"[a-z\.-]+$", tk):
                    for t in stk:
                        if len(t) < 3:
                            stk = tk
                            break
                    else:
                        stk = " ".join(stk)
                else:
                    stk = " ".join(stk)

            res.append(stk)

        return " ".join(self.english_normalize_(res))


def is_chinese(s):
    if s >= "\u4e00" and s <= "\u9fa5":
        return True
    else:
        return False


def is_number(s):
    if s >= "\u0030" and s <= "\u0039":
        return True
    else:
        return False


def is_alphabet(s):
    if ("\u0041" <= s <= "\u005a") or ("\u0061" <= s <= "\u007a"):
        return True
    else:
        return False


def naive_qie(txt):
    tks = []
    for t in txt.split():
        if tks and re.match(r".*[a-zA-Z]$", tks[-1]) and re.match(r".*[a-zA-Z]$", t):
            tks.append(" ")
        tks.append(t)
    return tks


_shared: Tokenizer | None = None


def tokenizer() -> Tokenizer:
    """The process's one tokenizer, built when something first asks.

    Shared because building it is not cheap: the dictionary is eight megabytes
    of word/frequency lines, and turning it into a trie takes seconds. Cached
    on the module rather than on a caller, so a keyword search and a parse use
    the same one.

    Raises:
        `DictionaryMissingError`: when the dictionary was never fetched.
    """
    global _shared
    if _shared is None:
        _shared = Tokenizer()
    return _shared


__all__ = [
    "DICTIONARY",
    "RES_DIR",
    "DictionaryMissingError",
    "Tokenizer",
    "available",
    "dictionary_path",
    "fold_diacritics",
    "is_alphabet",
    "is_chinese",
    "is_number",
    "naive_qie",
    "tokenizer",
]
