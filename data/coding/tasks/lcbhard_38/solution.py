from typing import List


def dice_sum_probability(n: int, dice_faces: List[int]) -> int:
    """ Given N dice where the i-th die shows a random integer between 1 and dice_faces[i-1] inclusive,
    find the probability (modulo 998244353) that there exists a subset of dice whose sum equals 10.
    
    The probability is returned as an integer z such that xz ≡ y (mod 998244353), where y/x is the
    probability in its irreducible fraction form.
    
    Args:
        n: Number of dice (1 ≤ n ≤ 100)
        dice_faces: List of n integers where dice_faces[i-1] is the maximum value for die i (1 ≤ dice_faces[i-1] ≤ 10^6)
    
    Returns:
        The probability modulo 998244353
    
    >>> dice_sum_probability(4, [1, 7, 2, 9])
    942786334
    >>> dice_sum_probability(7, [1, 10, 100, 1000, 10000, 100000, 1000000])
    996117877
    """
    pass
