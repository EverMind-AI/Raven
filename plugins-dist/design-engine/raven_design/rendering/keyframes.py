"""Select representative keyframes from animated render output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps

_ANALYSIS_EDGE = 192
_DUPLICATE_DISTANCE = 0.001


@dataclass
class _Frame:
    index: int
    candidate: dict[str, Any]
    image: Image.Image
    edges: Image.Image
    tokens: frozenset[str]
    visible_elements: int
    visible_text_chars: int
    complexity: float
    weight: float = 1.0
    quality: float = 0.5


def select_keyframes(
    candidates: list[dict[str, Any]],
    bundle_root: Path,
    limit: int,
) -> list[dict[str, Any]]:
    if not candidates or limit < 1:
        return []
    frames = [_load_frame(index, candidate, bundle_root) for index, candidate in enumerate(candidates)]
    if len(frames) == 1:
        return candidates
    distances = _distance_matrix(frames)
    _assign_weights(frames)
    _assign_quality(frames, distances)
    primary = next(
        (frame.index for frame in frames if frame.candidate.get("primary") is True),
        None,
    )
    selected = [_best_initial_frame(frames, distances, primary)]
    first = min(
        range(len(frames)),
        key=lambda index: (
            int(frames[index].candidate.get("at_ms") or 0),
            index,
        ),
    )
    if (
        len(selected) < min(limit, len(frames))
        and first not in selected
        and distances[first][selected[0]] > _DUPLICATE_DISTANCE
    ):
        selected.append(first)
    covered = [max(1.0 - distances[index][chosen] for chosen in selected) for index in range(len(frames))]
    time_span = max(
        1,
        max(int(frame.candidate.get("at_ms") or 0) for frame in frames)
        - min(int(frame.candidate.get("at_ms") or 0) for frame in frames),
    )
    while len(selected) < min(limit, len(frames)):
        best_index = None
        best_score = float("-inf")
        for frame in frames:
            if frame.index in selected:
                continue
            nearest_distance = min(distances[frame.index][chosen] for chosen in selected)
            if nearest_distance <= _DUPLICATE_DISTANCE:
                continue
            marginal = sum(
                other.weight
                * max(
                    0.0,
                    1.0 - distances[other.index][frame.index] - covered[other.index],
                )
                for other in frames
            )
            timestamp = int(frame.candidate.get("at_ms") or 0)
            temporal_novelty = (
                min(abs(timestamp - int(frames[chosen].candidate.get("at_ms") or 0)) for chosen in selected) / time_span
            )
            score = marginal * (0.85 + 0.10 * frame.quality + 0.05 * temporal_novelty)
            tie_break = (
                score,
                frame.quality,
                nearest_distance,
                -frame.index,
            )
            current = (
                best_score,
                frames[best_index].quality if best_index is not None else -1.0,
                (min(distances[best_index][chosen] for chosen in selected) if best_index is not None else -1.0),
                -best_index if best_index is not None else 0,
            )
            if tie_break > current:
                best_index = frame.index
                best_score = score
        if best_index is None:
            break
        selected.append(best_index)
        covered = [max(covered[index], 1.0 - distances[index][best_index]) for index in range(len(frames))]
    primary_selected = [index for index in selected if frames[index].candidate.get("primary") is True]
    supplemental = sorted(
        (index for index in selected if index not in primary_selected),
        key=lambda index: (
            int(frames[index].candidate.get("at_ms") or 0),
            index,
        ),
    )
    return [frames[index].candidate for index in [*primary_selected, *supplemental]]


def _load_frame(
    index: int,
    candidate: dict[str, Any],
    bundle_root: Path,
) -> _Frame:
    path = bundle_root / candidate["path"]
    with Image.open(path) as source:
        foreground = source.convert("RGBA")
        background = Image.new("RGBA", foreground.size, "white")
        background.alpha_composite(foreground)
        background.thumbnail(
            (_ANALYSIS_EDGE, _ANALYSIS_EDGE),
            Image.Resampling.LANCZOS,
        )
        image = Image.new("RGB", (_ANALYSIS_EDGE, _ANALYSIS_EDGE), "white")
        left = (_ANALYSIS_EDGE - background.width) // 2
        top = (_ANALYSIS_EDGE - background.height) // 2
        image.paste(background.convert("RGB"), (left, top))
    edges = ImageOps.grayscale(image).filter(ImageFilter.FIND_EDGES)
    edge_histogram = edges.histogram()
    edge_energy = sum(value * count for value, count in enumerate(edge_histogram)) / (edges.width * edges.height * 255)
    state = candidate.get("_state")
    if not isinstance(state, dict):
        state = {}
    tokens = frozenset(str(token) for token in state.get("visible_text_tokens", []) if str(token))
    return _Frame(
        index=index,
        candidate=candidate,
        image=image,
        edges=edges,
        tokens=tokens,
        visible_elements=int(state.get("visible_element_count") or 0),
        visible_text_chars=int(state.get("visible_text_chars") or 0),
        complexity=edge_energy,
    )


def _distance_matrix(frames: list[_Frame]) -> list[list[float]]:
    distances = [[0.0 for _ in range(len(frames))] for _ in range(len(frames))]
    for left in range(len(frames)):
        for right in range(left + 1, len(frames)):
            distance = _frame_distance(frames[left], frames[right])
            distances[left][right] = distance
            distances[right][left] = distance
    return distances


def _frame_distance(left: _Frame, right: _Frame) -> float:
    difference = ImageChops.difference(left.image, right.image)
    pixels = difference.width * difference.height
    histogram = difference.histogram()
    mean_delta = sum(
        value * count
        for channel in range(3)
        for value, count in enumerate(histogram[channel * 256 : (channel + 1) * 256])
    ) / (pixels * 3 * 255)
    changed = difference.convert("L").point(lambda value: 255 if value > 12 else 0).histogram()[255] / pixels
    edge_difference = ImageChops.difference(left.edges, right.edges)
    edge_delta = sum(value * count for value, count in enumerate(edge_difference.histogram())) / (pixels * 255)
    visual = min(
        1.0,
        0.55 * changed + 0.30 * min(1.0, mean_delta * 4) + 0.15 * min(1.0, edge_delta * 3),
    )
    distance = visual
    if left.tokens or right.tokens:
        union = left.tokens | right.tokens
        text_distance = 1.0 - len(left.tokens & right.tokens) / len(union)
        distance = max(distance, 0.35 * text_distance)
        if left.tokens != right.tokens:
            distance = max(distance, 0.05)
    maximum_elements = max(left.visible_elements, right.visible_elements)
    if maximum_elements:
        element_distance = abs(left.visible_elements - right.visible_elements) / maximum_elements
        distance = max(distance, 0.20 * element_distance)
    return min(1.0, distance)


def _assign_weights(frames: list[_Frame]) -> None:
    durations = [float(frame.candidate.get("duration_seconds") or 0) for frame in frames]
    positive = [duration for duration in durations if duration > 0]
    fallback = median(positive) if positive else 1.0
    for frame, duration in zip(frames, durations):
        frame.weight = duration if duration > 0 else fallback


def _assign_quality(
    frames: list[_Frame],
    distances: list[list[float]],
) -> None:
    complexities = _normalise([frame.complexity for frame in frames])
    elements = _normalise([float(frame.visible_elements) for frame in frames])
    text = _normalise([float(frame.visible_text_chars) for frame in frames])
    for index, frame in enumerate(frames):
        adjacent: list[float] = []
        if index:
            adjacent.append(distances[index][index - 1])
        if index + 1 < len(frames):
            adjacent.append(distances[index][index + 1])
        change = max(adjacent, default=0.0)
        stability = 1.0 / (1.0 + 20.0 * change)
        content = (complexities[index] + elements[index] + text[index]) / 3
        frame.quality = 0.65 * stability + 0.35 * content


def _normalise(values: list[float]) -> list[float]:
    low = min(values)
    high = max(values)
    if high - low <= 1e-9:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _best_initial_frame(
    frames: list[_Frame],
    distances: list[list[float]],
    primary: int | None,
) -> int:
    if primary is not None:
        return primary
    return max(
        range(len(frames)),
        key=lambda index: (
            sum(other.weight * (1.0 - distances[index][other.index]) for other in frames),
            frames[index].quality,
            -index,
        ),
    )
