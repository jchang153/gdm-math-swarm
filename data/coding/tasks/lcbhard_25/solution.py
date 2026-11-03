def sum_concatenated_permutations(n: int) -> int:
    """ Given a positive integer n, calculate the sum of concatenated permutations modulo 998244353.
    
    For each permutation P of (1, 2, ..., n), concatenate all numbers in P to form a single integer.
    For example, if P = (1, 20, 34), the concatenated result is 12034.
    
    Return the sum of all such concatenated integers for all n! permutations, modulo 998244353.
    
    Args:
        n: A positive integer (1 <= n <= 2 * 10^5)
    
    Returns:
        The sum of concatenated permutations modulo 998244353
    
    >>> sum_concatenated_permutations(3)
    1332
    >>> sum_concatenated_permutations(1)
    1
    """
    pass
