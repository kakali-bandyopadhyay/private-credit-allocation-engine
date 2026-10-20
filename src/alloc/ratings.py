"""Rating scale used for validation and rating-floor checks (higher index = lower credit quality)."""
RATING_SCALE = [
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
    "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
    "B+", "B", "B-", "CCC+", "CCC", "CCC-",
]


def rank(rating: str) -> int:
    """Position of a rating on the scale; raises ValueError for unknown ratings."""
    return RATING_SCALE.index(rating)


def meets_floor(rating: str, floor: str) -> bool:
    """True if `rating` is the same as or better than `floor`."""
    return rank(rating) <= rank(floor)
