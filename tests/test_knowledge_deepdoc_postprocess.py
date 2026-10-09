"""Turning a model's output into boxes and into words.

Both halves of this run without a model: one takes a probability map and hands
back quadrilaterals, the other takes a matrix of logits and hands back a
string. They are ported algorithms -- an off-by-one in the blank index or a
wrong unclip ratio does not fail, it quietly reads every scanned page a little
worse -- and they were reachable only through tests that skip wherever the
weights are not installed, which is every machine but a developer's.

Nothing here loads a session, so nothing here skips.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from raven.knowledge.parser.deepdoc._postprocess import CTCLabelDecode, DBPostProcess


def _bitmap(box: tuple[int, int, int, int], size: tuple[int, int] = (64, 64)) -> np.ndarray:
    """A probability map that is confident about one rectangle and nothing else."""
    height, width = size
    pred = np.zeros((height, width), dtype=np.float32)
    top, left, bottom, right = box
    pred[top:bottom, left:right] = 0.9
    return pred


# -- boxes out of a probability map --------------------------------


def test_one_bright_rectangle_comes_back_as_one_box() -> None:
    pred = _bitmap((10, 12, 30, 50))

    boxes, scores = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 64, 64)

    assert len(boxes) == 1
    assert len(scores) == 1
    left, top = boxes[0].min(axis=0)
    right, bottom = boxes[0].max(axis=0)
    # Wider than the drawn rectangle, because the box is unclipped outward: a
    # detector's map ends on the ink, and a crop that ends there cuts the
    # glyphs it was drawn around.
    assert left <= 12 and top <= 10
    assert right >= 50 and bottom >= 30


def test_an_empty_map_finds_nothing() -> None:
    pred = np.zeros((64, 64), dtype=np.float32)

    boxes, scores = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 64, 64)

    assert len(boxes) == 0 and len(scores) == 0


def test_a_box_is_scaled_to_the_page_it_was_found_on() -> None:
    """The map is the size the model wanted; the boxes have to be the size of
    the page a caller is going to crop."""
    pred = _bitmap((10, 12, 30, 50))

    small, _ = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 64, 64)
    large, _ = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 640, 640)

    assert large[0].max(axis=0)[0] > small[0].max(axis=0)[0] * 5


def test_a_faint_rectangle_is_below_the_threshold() -> None:
    """The mask decides what is a region at all; the score decides whether it
    survives. A map this faint has nothing confident enough to keep."""
    pred = np.full((64, 64), 0.2, dtype=np.float32)

    boxes, _ = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 64, 64)

    assert len(boxes) == 0


def test_two_rectangles_come_back_as_two_boxes() -> None:
    pred = _bitmap((5, 5, 15, 25))
    pred[40:55, 5:25] = 0.9

    boxes, _ = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 64, 64)

    assert len(boxes) == 2


def test_the_four_corners_come_back_in_one_order() -> None:
    """Every caller crops by index -- top-left first, clockwise -- so the order
    is part of the answer rather than a detail of how it was found."""
    pred = _bitmap((10, 12, 30, 50))

    boxes, _ = DBPostProcess().boxes_from_bitmap(pred, pred > 0.3, 64, 64)
    box = boxes[0]

    assert box.shape == (4, 2)
    assert box[0][0] <= box[1][0], "the second corner is to the right of the first"
    assert box[0][1] <= box[3][1], "the fourth corner is below the first"


# -- words out of a matrix of logits -------------------------------


def _logits(rows: list[int], width: int) -> np.ndarray:
    """One certain class per timestep, as a model would report it."""
    out = np.zeros((1, len(rows), width), dtype=np.float32)
    for step, index in enumerate(rows):
        out[0, step, index] = 1.0
    return out


def test_the_timesteps_decode_to_their_characters() -> None:
    """With no dictionary the decoder carries its own alphabet: blank first,
    then 0-9 and a-z. So index 1 is '0' and index 11 is 'a'."""
    decoder = CTCLabelDecode()

    [(text, score)] = decoder(_logits([11, 12, 13], len(decoder.character)))

    assert text == "abc"
    assert score > 0.9


def test_a_character_held_across_timesteps_is_written_once() -> None:
    """What the blank is for: a model reports a letter for as long as it sees
    it, and 'aaa' is one 'a' unless a blank separates them."""
    decoder = CTCLabelDecode()

    [(text, _)] = decoder(_logits([11, 11, 11], len(decoder.character)))

    assert text == "a"


def test_a_blank_between_two_of_the_same_letter_keeps_both() -> None:
    decoder = CTCLabelDecode()

    [(text, _)] = decoder(_logits([11, 0, 11], len(decoder.character)))

    assert text == "aa"


def test_blanks_alone_decode_to_nothing() -> None:
    decoder = CTCLabelDecode()

    [(text, _)] = decoder(_logits([0, 0, 0], len(decoder.character)))

    assert text == ""


def test_the_score_is_the_confidence_of_what_was_kept() -> None:
    """Averaged over the timesteps that produced characters, not over every
    timestep: a long run of blanks either side of a word would otherwise make
    a certain reading look uncertain."""
    decoder = CTCLabelDecode()
    logits = _logits([11, 12], len(decoder.character))
    logits[0, 0, 11] = 0.5

    [(text, score)] = decoder(logits)

    assert text == "ab"
    assert 0.7 < score < 0.8


def test_every_line_in_a_batch_is_decoded() -> None:
    """The recognizer reads a page's crops in batches, and the result has to
    line up with the crops that went in."""
    decoder = CTCLabelDecode()
    width = len(decoder.character)
    batch = np.concatenate([_logits([11], width), _logits([12], width)], axis=0)

    assert [text for text, _ in decoder(batch)] == ["a", "b"]


# -- the whole step, as the detector calls it ----------------------


def _maps(box: tuple[int, int, int, int], size: tuple[int, int] = (64, 64)) -> np.ndarray:
    """What a session hands back: one map, one channel, batch of one."""
    return _bitmap(box, size)[None, None, :, :]


def test_the_step_answers_one_entry_per_image() -> None:
    boxes = DBPostProcess()({"maps": _maps((10, 12, 30, 50))}, [[64, 64, 1.0, 1.0]])

    assert len(boxes) == 1
    assert len(boxes[0]["points"]) == 1


def test_the_step_scales_each_image_by_its_own_shape() -> None:
    """The shape list is how a batch of differently sized pages comes back to
    its own coordinates."""
    maps = np.concatenate([_maps((10, 12, 30, 50)), _maps((10, 12, 30, 50))], axis=0)

    boxes = DBPostProcess()({"maps": maps}, [[64, 64, 1.0, 1.0], [640, 640, 0.1, 0.1]])

    assert len(boxes) == 2
    assert boxes[1]["points"][0].max() > boxes[0]["points"][0].max() * 5


def test_a_box_type_that_is_neither_is_refused() -> None:
    """Rather than returning an empty page, which reads as a blank scan."""
    with pytest.raises(ValueError, match="quad"):
        DBPostProcess(box_type="rect")({"maps": _maps((10, 12, 30, 50))}, [[64, 64, 1.0, 1.0]])


def test_dilation_keeps_a_broken_stroke_as_one_region() -> None:
    """Faint ink comes back as a map with gaps in it, and two halves of one
    word are two crops unless the mask is grown first."""
    maps = _maps((10, 10, 30, 50))
    maps[0, 0, 10:30, 29:30] = 0.0

    split = DBPostProcess()({"maps": maps}, [[64, 64, 1.0, 1.0]])
    joined = DBPostProcess(use_dilation=True)({"maps": maps}, [[64, 64, 1.0, 1.0]])

    assert len(split[0]["points"]) == 2
    assert len(joined[0]["points"]) == 1


def _fill(pred: np.ndarray, corners: np.ndarray) -> None:
    cv2.fillPoly(pred, corners.reshape(1, -1, 2), 0.9)


def test_the_polygon_path_follows_the_shape_rather_than_boxing_it() -> None:
    """The other box type, kept for a caller that wants the region's outline
    and not the rectangle around it."""
    pred = _bitmap((10, 12, 30, 50))

    polys, scores = DBPostProcess(box_type="poly").polygons_from_bitmap(pred, pred > 0.3, 64, 64)

    assert len(polys) == 1 and len(scores) == 1
    assert len(polys[0]) >= 4


def test_the_slow_score_measures_the_shape_and_not_its_bounding_box() -> None:
    """Both mask by a polygon; what differs is which polygon they are given.
    The fast mode scores the rectangle around a region, so a region that is
    not one -- a heading over a wrapped line, a triangle here -- drags in the
    blank beside it and scores lower than the ink deserves."""
    pred = np.zeros((64, 64), dtype=np.float32)
    corners = np.array([[10, 10], [50, 10], [10, 50]], dtype=np.int32)
    _fill(pred, corners)
    step = DBPostProcess(score_mode="slow")

    along = step.box_score_slow(pred, corners)
    across = step.box_score_fast(pred, np.array([[10, 10], [50, 10], [50, 50], [10, 50]], dtype=np.float32))

    assert along > across + 0.3


# -- a dictionary, and a script that runs the other way ------------


def _dict_file(path, chars: list[str]):
    path.write_text("\n".join(chars), encoding="utf-8")
    return path


def test_a_dictionary_on_disk_replaces_the_built_in_alphabet(tmp_path) -> None:
    """The alphabet a model was trained on is a file beside its weights; the
    built-in one is only a fallback."""
    path = _dict_file(tmp_path / "keys.txt", ["x", "y", "z"])

    decoder = CTCLabelDecode(character_dict_path=str(path), use_space_char=True)

    assert decoder.character[0] == "blank", "the blank stays at index zero"
    assert decoder.character[1:] == ["x", "y", "z", " "]


def test_a_right_to_left_dictionary_reverses_what_was_read(tmp_path) -> None:
    """A recognizer reads every script left to right; for one written the
    other way the runs come back in the wrong order, and the latin inside a
    run -- a number, a unit -- must not be reversed with them."""
    path = _dict_file(tmp_path / "arabic_dict.txt", ["#", "a", "b"])
    decoder = CTCLabelDecode(character_dict_path=str(path))

    [(text, _)] = decoder(_logits([2, 3, 1], len(decoder.character)))

    assert text == "#ab"


def test_a_left_to_right_dictionary_is_left_alone(tmp_path) -> None:
    path = _dict_file(tmp_path / "latin_dict.txt", ["#", "a", "b"])
    decoder = CTCLabelDecode(character_dict_path=str(path))

    [(text, _)] = decoder(_logits([2, 3, 1], len(decoder.character)))

    assert text == "ab#"
