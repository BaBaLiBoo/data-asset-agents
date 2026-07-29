from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def jaccard(left: Iterable[str], right: Iterable[str]) -> float | None:
    left_set = {item for item in left if item}
    right_set = {item for item in right if item}
    if not left_set and not right_set:
        return None
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def multiset_jaccard(left: Iterable[str], right: Iterable[str]) -> float | None:
    left_counter = Counter(item for item in left if item)
    right_counter = Counter(item for item in right if item)
    if not left_counter and not right_counter:
        return None
    if not left_counter or not right_counter:
        return 0.0
    keys = set(left_counter) | set(right_counter)
    intersection = sum(min(left_counter[key], right_counter[key]) for key in keys)
    union = sum(max(left_counter[key], right_counter[key]) for key in keys)
    return intersection / union if union else None


def weighted_available(
    values: dict[str, float | None],
    weights: dict[str, float],
) -> tuple[float | None, float]:
    active = [key for key, value in values.items() if value is not None and weights.get(key, 0) > 0]
    denominator = sum(weights[key] for key in active)
    if not active or denominator <= 0:
        return None, 0.0
    score = sum(weights[key] * float(values[key]) for key in active) / denominator
    return clamp(score), clamp(denominator / sum(weights.values()))

