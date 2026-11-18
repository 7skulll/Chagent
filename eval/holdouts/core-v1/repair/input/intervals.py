def merge_intervals(intervals):
    """Merge closed intervals."""
    if not intervals:
        return []
    result = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start < result[-1][1]:
            result[-1][1] = end
        else:
            result.append([start, end])
    return result
