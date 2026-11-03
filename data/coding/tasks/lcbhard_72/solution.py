from typing import List, Optional, Tuple


def find_triple_sum(n: int, x: int, a: List[int]) -> Optional[Tuple[int, int, int]]:
    """ Given an integer sequence A and an integer X, find one triple of indices (i, j, k)
    such that:
    - 1 <= i < j < k <= n
    - A[i-1] + A[j-1] + A[k-1] = x
    
    Note: The indices returned are 1-based (matching the problem statement).
    
    Args:
        n: The length of the sequence (3 <= n <= 10^6)
        x: The target sum (1 <= x <= 10^6)
        a: The integer sequence where each element satisfies 1 <= a[i] <= x
    
    Returns:
        A tuple (i, j, k) of 1-based indices if such a triple exists, None otherwise.
    
    >>> find_triple_sum(5, 16, [1, 8, 5, 10, 13])
    (1, 3, 4)
    >>> find_triple_sum(5, 20, [1, 8, 5, 10, 13])
    None
    >>> find_triple_sum(3, 6, [2, 2, 2])
    (1, 2, 3)
    """
    pass
