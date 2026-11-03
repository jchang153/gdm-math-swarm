from typing import List


def count_valid_permutations(n: int, requirements: List[List[int]]) -> int:
    """ Count the number of permutations of [0, 1, 2, ..., n-1] such that for each requirement
    [end_i, cnt_i], the prefix perm[0:end_i+1] has exactly cnt_i inversions.
    
    An inversion is a pair of indices (i, j) where i < j and perm[i] > perm[j].
    
    Return the count modulo 10^9 + 7.
    
    Args:
        n: The size of the permutation (2 <= n <= 300)
        requirements: List of [end_index, inversion_count] pairs where:
            - 0 <= end_index <= n-1
            - 0 <= inversion_count <= 400
            - All end_indices are unique
            - At least one requirement has end_index == n-1
    
    Returns:
        The number of valid permutations modulo 10^9 + 7
    
    >>> count_valid_permutations(3, [[2, 2], [0, 0]])
    2
    >>> count_valid_permutations(3, [[2, 2], [1, 1], [0, 0]])
    1
    >>> count_valid_permutations(2, [[0, 0], [1, 0]])
    1
    """
    pass
