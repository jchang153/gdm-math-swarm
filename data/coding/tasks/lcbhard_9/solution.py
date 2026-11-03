def count_ways_to_stair(k: int) -> int:
    """ Alice starts on stair 1 and wants to reach stair k. She has a jump value starting at 0.
    From stair i, she can perform two types of operations:
    1. Go down to stair i-1 (cannot be used consecutively or on stair 0)
    2. Go up to stair i + 2^jump, then increment jump by 1
    
    Return the total number of ways Alice can reach stair k.
    Note that Alice can reach stair k multiple times and continue operations.
    
    Args:
        k: The target stair number (0 <= k <= 10^9)
    
    Returns:
        The number of distinct ways to reach stair k
    
    >>> count_ways_to_stair(0)
    2
    >>> count_ways_to_stair(1)
    4
    >>> count_ways_to_stair(512)
    1
    """
    pass
