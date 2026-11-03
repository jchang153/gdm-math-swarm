from typing import List


def count_subsequence_pairs(nums: List[int]) -> int:
    """ Find the number of pairs of non-empty subsequences (seq1, seq2) of nums that satisfy:
    1. The subsequences seq1 and seq2 are disjoint (no index of nums is common between them)
    2. The GCD of the elements of seq1 is equal to the GCD of the elements of seq2
    
    Return the total number of such pairs modulo 10^9 + 7.
    
    Args:
        nums: An integer array where 1 <= len(nums) <= 200 and 1 <= nums[i] <= 200
    
    Returns:
        The number of valid subsequence pairs modulo 10^9 + 7
    
    >>> count_subsequence_pairs([1, 2, 3, 4])
    10
    >>> count_subsequence_pairs([10, 20, 30])
    2
    >>> count_subsequence_pairs([1, 1, 1, 1])
    50
    """
    pass
