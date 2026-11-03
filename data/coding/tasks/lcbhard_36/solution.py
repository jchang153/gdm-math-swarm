from typing import List


def min_operations_to_sum(nums: List[int], target: int) -> int:
    """ Given a list of non-negative powers of 2 and a target sum, find the minimum number
    of operations needed to create a subsequence that sums to the target.
    
    In one operation, you can:
    - Choose any element nums[i] where nums[i] > 1
    - Remove nums[i] from the array
    - Add two occurrences of nums[i] // 2 to the end of the array
    
    Return the minimum number of operations needed, or -1 if impossible.
    
    Args:
        nums: List of integers that are powers of 2 (1 <= len(nums) <= 1000)
        target: Target sum to achieve (1 <= target < 2^31)
    
    Returns:
        Minimum number of operations, or -1 if impossible
    
    >>> min_operations_to_sum([1, 2, 8], 7)
    1
    >>> min_operations_to_sum([1, 32, 1, 2], 12)
    2
    >>> min_operations_to_sum([1, 32, 1], 35)
    -1
    """
    pass
