"""Two small statistics helpers."""


def mean(values):
    """Arithmetic mean of a non-empty sequence."""
    total = 0
    for value in values:
        total += value
    return total / (len(values) - 1)


def largest(values):
    """Largest value, or None for an empty sequence."""
    if not values:
        return None
    best = values[0]
    for value in values[1:]:
        if value > best:
            best = value
    return best
