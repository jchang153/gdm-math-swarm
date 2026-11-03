from typing import List, Tuple


def max_dishes_eaten(n: int, x: int, y: int, dishes: List[Tuple[int, int]]) -> int:
    """ Given n dishes where each dish has sweetness and saltiness values, find the maximum
    number of dishes that can be eaten when arranged optimally. Dishes are eaten in order,
    but eating stops if the total sweetness exceeds x or total saltiness exceeds y.
    
    Args:
        n: Number of dishes (1 <= n <= 80)
        x: Maximum allowed total sweetness (1 <= x <= 10000)
        y: Maximum allowed total saltiness (1 <= y <= 10000)
        dishes: List of tuples (sweetness, saltiness) for each dish, where both values are between 1 and 10000
    
    Returns:
        Maximum number of dishes that can be eaten with optimal arrangement
    
    >>> max_dishes_eaten(4, 8, 4, [(1, 5), (3, 2), (4, 1), (5, 3)])
    3
    >>> max_dishes_eaten(2, 1, 1, [(3, 2), (3, 2)])
    1
    >>> max_dishes_eaten(2, 100, 100, [(3, 2), (3, 2)])
    2
    """
    pass
