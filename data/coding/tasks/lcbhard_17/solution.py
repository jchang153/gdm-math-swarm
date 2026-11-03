def sum_of_scores(n: int, m: int) -> int:
    """ Calculate the sum of scores of all good sequences modulo 998244353.
    
    A good sequence is an integer sequence of length between 1 and n (inclusive),
    where each element is between 1 and m (inclusive).
    
    The score of a good sequence is the number of positive divisors of X,
    where X is the product of all elements in the sequence.
    
    The function computes the sum of scores of all possible good sequences.
    There are sum(m^k for k in range(1, n+1)) such sequences in total.
    
    Args:
        n: Maximum length of sequences (1 <= n <= 10^18)
        m: Maximum value of elements (1 <= m <= 16)
    
    Returns:
        The sum of all scores modulo 998244353
    
    >>> sum_of_scores(1, 7)
    16
    >>> sum_of_scores(3, 11)
    16095
    >>> sum_of_scores(81131, 14)
    182955659
    """
    pass
