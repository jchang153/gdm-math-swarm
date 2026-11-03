def count_k_palindromic_numbers(n: int, k: int) -> int:
    """ Count the number of good integers containing exactly n digits.
    
    An integer x is called k-palindromic if:
    - x is a palindrome (reads the same forwards and backwards)
    - x is divisible by k
    
    An integer is called good if its digits can be rearranged to form a k-palindromic integer.
    
    Note: Integers must not have leading zeros, neither before nor after rearrangement.
    
    Args:
        n: The number of digits (1 <= n <= 10)
        k: The divisor for k-palindromic property (1 <= k <= 9)
    
    Returns:
        The count of good integers with exactly n digits
    
    >>> count_k_palindromic_numbers(3, 5)
    27
    >>> count_k_palindromic_numbers(1, 4)
    2
    >>> count_k_palindromic_numbers(5, 6)
    2468
    """
    pass
