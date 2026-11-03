from typing import List


def min_cost_to_sort(n: int, permutation: List[int]) -> int:
    """ Given a permutation of integers from 1 to n, find the minimum total cost to sort it in 
    ascending order. You can repeatedly swap adjacent elements, where swapping elements at 
    positions i and i+1 (1-indexed) costs i.
    
    Args:
        n: The length of the permutation
        permutation: A list of integers from 1 to n in some order
    
    Returns:
        The minimum total cost required to sort the permutation in ascending order
    
    >>> min_cost_to_sort(3, [3, 2, 1])
    4
    >>> min_cost_to_sort(5, [2, 4, 1, 3, 5])
    6
    >>> min_cost_to_sort(2, [1, 2])
    0
    """
    pass
