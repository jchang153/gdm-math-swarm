from typing import List


def count_arithmetic_subsequences(n: int, a: List[int]) -> List[int]:
    """ Given a sequence of integers, count the number of subsequences of each length k (from 1 to n)
    that form arithmetic sequences. Two subsequences are distinguished if they are taken from 
    different positions, even if they are equal as sequences.
    
    An arithmetic sequence is one where consecutive elements have the same difference.
    Return the counts modulo 998244353.
    
    Args:
        n: The length of the sequence (1 <= n <= 80)
        a: The sequence of integers (1 <= a[i] <= 10^9)
    
    Returns:
        A list of n integers, where the i-th element is the count of arithmetic subsequences 
        of length i+1, modulo 998244353.
    
    >>> count_arithmetic_subsequences(5, [1, 2, 3, 2, 3])
    [5, 10, 3, 0, 0]
    >>> count_arithmetic_subsequences(4, [1, 2, 3, 4])
    [4, 6, 2, 1]
    >>> count_arithmetic_subsequences(1, [100])
    [1]
    """
    pass
