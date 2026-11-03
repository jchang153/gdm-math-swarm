from typing import List, Tuple


def count_ball_sets(n: int, positions: List[Tuple[int, int]]) -> int:
    """ Given n balls on a 2D plane where ball i is at position (x_i, y_i),
    and the x-coordinates and y-coordinates are permutations of (1, 2, ..., n),
    count the number of possible sets of balls that can remain after performing operations.
    
    An operation consists of:
    - Choose one of the remaining balls, say ball k at position (x_k, y_k)
    - Remove all remaining balls i where either:
      * x_i < x_k and y_i < y_k (lower-left quadrant), or
      * x_i > x_k and y_i > y_k (upper-right quadrant)
    
    Return the count modulo 998244353.
    
    Args:
        n: Number of balls (1 <= n <= 300)
        positions: List of (x, y) coordinates for each ball, where x and y values
                  form permutations of (1, 2, ..., n)
    
    Returns:
        Number of possible final sets of balls modulo 998244353
    
    >>> count_ball_sets(3, [(1, 3), (2, 1), (3, 2)])
    3
    >>> count_ball_sets(4, [(4, 2), (2, 1), (3, 3), (1, 4)])
    3
    """
    pass
