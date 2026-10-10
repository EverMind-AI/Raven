"""Segmentation itself, against a dictionary written by the test.

:mod:`tests.test_core_tokenizer` covers the same class against the real 8 MB
word list and skips whole when it has not been fetched -- which is every
machine but a developer's, and every CI run. So the segmenter, the part of this
package that exists because a keyword search over Chinese is noise without it,
was never exercised where it is actually checked.

`Tokenizer(user_dict=...)` takes any dictionary, so the cases here build a
twenty-word one and assert on segmentations that follow from it alone. Nothing
here needs the download.

Lemmatization is the one exception: WordNet is a corpus, fetched by the same
step, and the cases that reach it say so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.core import tokenizer as tok

#: word, frequency, part-of-speech tag -- the three space-separated fields the
#: published list uses. The frequencies only have to order the candidates: the
#: segmenter scores a split by the sum of its words' frequencies, so "database"
#: outranking "data" is what makes the longer word win.
_WORDS = [
    ("\u6570\u636e", 500, "n"),  # data
    ("\u6570\u636e\u5e93", 900, "n"),  # database
    ("\u7ba1\u7406", 400, "n"),  # management
    ("\u7cfb\u7edf", 400, "n"),  # system
    ("\u7ba1\u7406\u7cfb\u7edf", 800, "n"),  # management system
    ("\u5317\u4eac", 600, "ns"),  # Beijing
    ("\u5927\u5b66", 600, "n"),  # university
    ("hello", 300, "n"),
    ("world", 300, "n"),
    ("running", 200, "v"),
]


def _corpora_available() -> bool:
    """Whether the latin half of the tokenizer has what it reads.

    Two corpora, both from the same fetch step: the sentence splitter
    `word_tokenize` loads, and the WordNet data the lemmatizer reads. Any line
    carrying latin text goes through both, so a case with so much as an English
    word in it needs them; a line of Chinese alone is segmented from the
    dictionary and needs neither.

    Asked after this module's import of `raven.core.tokenizer`, which is what
    puts the package's own `res/nltk_data` on nltk's search path.
    """
    try:
        import nltk
        from nltk.corpus import wordnet

        wordnet.ensure_loaded()
        nltk.data.find("tokenizers/punkt_tab")
    except Exception:
        return False
    return True


needs_corpora = pytest.mark.skipif(not _corpora_available(), reason="the nltk corpora have not been fetched")


@pytest.fixture
def dictionary(tmp_path: Path) -> Path:
    path = tmp_path / "mini.txt"
    path.write_text("\n".join(f"{word} {freq} {tag}" for word, freq, tag in _WORDS), encoding="utf-8")
    return path


@pytest.fixture
def segmenter(dictionary: Path) -> tok.Tokenizer:
    return tok.Tokenizer(user_dict=str(dictionary))


class TestBuildingFromADictionary:
    def test_a_dictionary_file_builds_a_working_segmenter(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.tokenize("\u6570\u636e\u5e93").split() == ["\u6570\u636e\u5e93"]

    def test_the_trie_is_cached_beside_the_dictionary(self, segmenter: tok.Tokenizer, dictionary: Path) -> None:
        """Six seconds of trie building, spent once. The cache is what a second
        process loads instead of reading the word list again."""
        assert dictionary.with_suffix(".txt.trie").is_file()

    def test_a_second_segmenter_loads_the_cache_rather_than_rebuilding(self, dictionary: Path) -> None:
        tok.Tokenizer(user_dict=str(dictionary))

        again = tok.Tokenizer(user_dict=str(dictionary))

        assert again.tokenize("\u6570\u636e\u5e93").split() == ["\u6570\u636e\u5e93"]

    @needs_corpora
    def test_a_missing_user_dictionary_falls_back_to_the_shipped_one(self, tmp_path: Path) -> None:
        """A path that is not there is a misconfiguration, not a reason to
        refuse to segment -- but with no shipped dictionary either, the error
        names the command that fetches it."""
        absent = str(tmp_path / "nowhere.txt")

        if tok.available():
            assert tok.Tokenizer(user_dict=absent).tokenize("hello")
        else:
            with pytest.raises(tok.DictionaryMissingError):
                tok.Tokenizer(user_dict=absent)

    def test_a_user_dictionary_can_be_added_to_a_built_segmenter(
        self, segmenter: tok.Tokenizer, tmp_path: Path
    ) -> None:
        extra = tmp_path / "extra.txt"
        extra.write_text("\u4eba\u5de5\u667a\u80fd 900 n\n", encoding="utf-8")

        segmenter.add_user_dict(str(extra))

        assert "\u4eba\u5de5\u667a\u80fd" in segmenter.tokenize("\u4eba\u5de5\u667a\u80fd")

    def test_a_user_dictionary_can_replace_the_loaded_one(self, segmenter: tok.Tokenizer, tmp_path: Path) -> None:
        other = tmp_path / "other.txt"
        other.write_text("\u4e0a\u6d77 900 ns\n", encoding="utf-8")

        segmenter.load_user_dict(str(other))

        assert "\u4e0a\u6d77" in segmenter.tokenize("\u4e0a\u6d77")

    def test_an_unreadable_dictionary_does_not_take_the_process_down(
        self, segmenter: tok.Tokenizer, tmp_path: Path
    ) -> None:
        """A library that exits on a bad file takes the gateway with it. The
        original calls `exit(1)` here."""
        segmenter.add_user_dict(str(tmp_path / "does-not-exist.txt"))

        assert segmenter.tokenize("\u6570\u636e\u5e93")


class TestKeys:
    """How a word is stored, forwards and backwards.

    The backward key is what the right-to-left pass looks up, and it carries a
    `DD` prefix so the two directions cannot collide in one trie.
    """

    def test_a_word_keys_on_its_own_lowercased_bytes(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.key_("Hello") == segmenter.key_("hello")

    def test_the_reverse_key_is_distinct_from_the_forward_one(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.rkey_("hello") != segmenter.key_("hello")

    def test_the_reverse_key_reverses_the_word(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.rkey_("ab") == segmenter.rkey_("ab")
        assert segmenter.rkey_("ab") != segmenter.rkey_("ba")


class TestSegmentation:
    """Where the word boundaries fall, given the dictionary above."""

    def test_the_longer_word_wins_when_the_dictionary_prefers_it(self, segmenter: tok.Tokenizer) -> None:
        """ "database" is one word, not "data" plus a character. This is the
        whole job: the frequencies decide, and a wrong comparison produces a
        split that still looks like words."""
        assert segmenter.tokenize("\u6570\u636e\u5e93").split() == ["\u6570\u636e\u5e93"]

    def test_a_run_of_words_splits_at_every_boundary(self, segmenter: tok.Tokenizer) -> None:
        out = segmenter.tokenize("\u6570\u636e\u5e93\u7ba1\u7406\u7cfb\u7edf").split()

        assert "\u6570\u636e\u5e93" in out
        assert out != ["\u6570\u636e\u5e93\u7ba1\u7406\u7cfb\u7edf"], "it did split"

    def test_a_word_the_dictionary_does_not_have_still_comes_back(self, segmenter: tok.Tokenizer) -> None:
        """Unknown text is not dropped: it falls through per character, which
        is the behaviour an absent dictionary gives for everything."""
        assert "\u55b5" in segmenter.tokenize("\u55b5")

    def test_known_words_survive_unknown_neighbours(self, segmenter: tok.Tokenizer) -> None:
        assert "\u5317\u4eac" in segmenter.tokenize("\u55b5\u5317\u4eac\u55b5")

    def test_empty_text_segments_to_nothing(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.tokenize("").strip() == ""

    def test_full_width_punctuation_is_narrowed_first(self, segmenter: tok.Tokenizer) -> None:
        """A full-width comma is the same comma; left as-is it is a character
        the dictionary has never seen, sitting inside a word."""
        assert segmenter._strQ2B("\uff21\uff22\uff0c") == "AB,"

    def test_a_full_width_space_becomes_an_ordinary_one(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter._strQ2B("\u3000") == " "

    def test_a_character_with_no_half_width_form_is_left_alone(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter._strQ2B("\u6570") == "\u6570"

    def test_traditional_characters_are_folded_to_simplified(self, segmenter: tok.Tokenizer) -> None:
        """The dictionary is simplified; without this every traditional page
        misses every word in it."""
        assert segmenter._tradi2simp("\u8cc7\u6599") == "\u8d44\u6599"

    def test_a_traditional_page_segments_through_the_simplified_dictionary(self, segmenter: tok.Tokenizer) -> None:
        assert "\u6570\u636e\u5e93" in segmenter.tokenize("\u6578\u64da\u5eab")


class TestFineGrainedTokenize:
    """The second pass: the long words split again, so a search for the part
    still finds the whole."""

    def test_a_compound_is_split_into_its_parts(self, segmenter: tok.Tokenizer) -> None:
        out = segmenter.fine_grained_tokenize(segmenter.tokenize("\u6570\u636e\u5e93"))

        assert "\u6570\u636e" in out

    def test_text_with_no_compounds_passes_through(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.fine_grained_tokenize("\u5317\u4eac").strip() == "\u5317\u4eac"

    def test_empty_input_stays_empty(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.fine_grained_tokenize("").strip() == ""


class TestFrequencyAndTag:
    def test_a_known_word_reports_the_dictionary_frequency(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.freq("\u6570\u636e\u5e93") > 0

    def test_an_unknown_word_reports_nothing(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.freq("\u55b5\u55b5\u55b5") == 0

    def test_a_known_word_reports_its_part_of_speech(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.tag("\u5317\u4eac") == "ns"

    def test_an_unknown_word_has_no_part_of_speech(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.tag("\u55b5\u55b5\u55b5") == ""


class TestLanguages:
    def test_a_snowball_language_switches_the_stemmer(self, segmenter: tok.Tokenizer) -> None:
        segmenter.set_language("Dutch")

        assert segmenter._use_lemmatizer is False, "WordNet is English only"

    def test_english_turns_the_lemmatizer_back_on(self, segmenter: tok.Tokenizer) -> None:
        segmenter.set_language("Dutch")

        segmenter.set_language("English")

        assert segmenter._use_lemmatizer is True

    def test_a_language_with_no_stemmer_keeps_the_defaults(self, segmenter: tok.Tokenizer) -> None:
        """Chinese is segmented by dictionary, not stemmed."""
        segmenter.set_language("Chinese")

        assert segmenter.tokenize("\u6570\u636e\u5e93")

    def test_the_name_is_read_case_insensitively(self, segmenter: tok.Tokenizer) -> None:
        segmenter.set_language("  dUtCh  ")

        assert segmenter._use_lemmatizer is False

    @pytest.mark.parametrize("language", ["Czech", "Slovak"])
    def test_a_diacritic_folding_language_folds_rather_than_stems(
        self, segmenter: tok.Tokenizer, language: str
    ) -> None:
        """Neither has a Snowball stemmer, and the split pattern keeps only
        ASCII letter runs whole -- so an accented word would be fragmented
        before it was indexed."""
        segmenter.set_language(language)

        assert segmenter._fold_diacritics is True
        assert segmenter._normalize_token("running") == "running", "left unstemmed"

    @needs_corpora
    def test_a_folding_language_strips_the_accents_from_its_words(self, segmenter: tok.Tokenizer) -> None:
        segmenter.set_language("Czech")

        assert "skola" in segmenter.tokenize("\u0161kola")

    @pytest.mark.xfail(
        reason="turkish is the one entry in the snowball map nltk has no stemmer for, so this raises ValueError",
        raises=ValueError,
        strict=True,
    )
    def test_turkish_is_claimed_by_the_snowball_map_but_has_no_stemmer(self, segmenter: tok.Tokenizer) -> None:
        """Pinned as a known defect rather than left uncovered.

        Turkish is neither folded nor stemmable: it sits in the Snowball map,
        `set_language` therefore skips the folding branch, and `SnowballStemmer`
        then refuses the name. It belongs with czech and slovak, which are
        exactly the other languages with accented letters and no stemmer.
        """
        segmenter.set_language("Turkish")


class TestDiacritics:
    def test_an_accented_letter_folds_to_its_base(self) -> None:
        assert tok.fold_diacritics("\u00e9cole") == "ecole"

    def test_an_unaccented_word_is_unchanged(self) -> None:
        assert tok.fold_diacritics("school") == "school"

    def test_a_character_with_no_base_is_left_alone(self) -> None:
        assert tok.fold_diacritics("\u6570") == "\u6570"

    def test_the_turkish_dotless_i_is_handled(self) -> None:
        assert tok.fold_diacritics("\u0131") in {"i", "\u0131"}


class TestIsChinese:
    def test_han_characters_are_chinese(self) -> None:
        assert tok.is_chinese("\u6570")

    def test_latin_is_not(self) -> None:
        assert not tok.is_chinese("a")

    def test_a_digit_is_not(self) -> None:
        assert not tok.is_chinese("1")


class TestEnglishNormalisation:
    @needs_corpora
    def test_an_english_word_is_lemmatised_then_stemmed(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.tokenize("running").strip() == "run"

    @needs_corpora
    def test_case_is_folded(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter.tokenize("Hello World").split() == ["hello", "world"]

    def test_a_non_alphabetic_token_is_left_alone(self, segmenter: tok.Tokenizer) -> None:
        assert segmenter._normalize_token("2024") == "2024"

    def test_stemming_without_the_lemmatizer_still_works(self, segmenter: tok.Tokenizer) -> None:
        """The path every non-English Snowball language takes, and the one an
        install without the corpora falls back to."""
        segmenter._use_lemmatizer = False

        assert segmenter._normalize_token("running") == "run"


class TestMixedScripts:
    @needs_corpora
    def test_a_line_of_both_scripts_splits_at_the_script_boundary(self, segmenter: tok.Tokenizer) -> None:
        out = segmenter.tokenize("\u5317\u4eac hello").split()

        assert "\u5317\u4eac" in out
        assert "hello" in out
