from typing import List


def max_1122_subarray_length(n: int, a: List[int]) -> int:
    """ Given a list of positive integers, find the maximum length of a contiguous subarray 
    that forms a 1122 sequence.
    
    A 1122 sequence is defined as a sequence that:
    1. Has even length
    2. For each i from 1 to len/2, elements at positions 2i-1 and 2i are equal (1-indexed)
    3. Each positive integer appears either 0 times or exactly 2 times in the sequence
    
    Args:
        n: The length of the array (1 <= n <= 2*10^5)
        a: The array of positive integers (1 <= a[i] <= n)
    
    Returns:
        The maximum length of a contiguous subarray that is a 1122 sequence
    
    >>> max_1122_subarray_length(8, [2, 3, 1, 1, 2, 2, 1, 1])
    4
    >>> max_1122_subarray_length(3, [1, 2, 2])
    2
    >>> max_1122_subarray_length(1, [1])
    0
    """
    pass
