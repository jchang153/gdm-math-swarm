def find_largest_400_number(a: int) -> int:
    """ Find the largest 400 number not exceeding a.
    
    A positive integer N is a 400 number if and only if:
    1. N has exactly 2 distinct prime factors.
    2. For each prime factor p of N, p divides N an even number of times.
       (i.e., the maximum k such that p^k divides N is even)
    
    Args:
        a: An integer where 36 <= a <= 10^12
    
    Returns:
        The largest 400 number not exceeding a
    
    >>> find_largest_400_number(404)
    400
    >>> find_largest_400_number(36)
    36
    >>> find_largest_400_number(60)
    36
    >>> find_largest_400_number(1000000000000)
    1000000000000
    >>> find_largest_400_number(123456789)
    123454321
    """
    pass
