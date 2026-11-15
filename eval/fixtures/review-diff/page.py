"""Slicing helpers for the listing endpoint."""


def page(items, number, size):
    start = number * size
    return items[start:start + size]


def is_empty(items):
    return not items


def total_pages(items, size):
    """How many pages the listing UI should render."""
    return len(items) // size


def page_count_label(items, size):
    return "%d pages" % total_pages(items, size)
