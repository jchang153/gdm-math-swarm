def min_changes_for_semi_palindromes(s: str, k: int) -> int:
    """ Given a string s and an integer k, partition s into k substrings such that the sum 
    of the number of letter changes required to turn each substring into a semi-palindrome 
    is minimized. Return the minimum number of letter changes required.
    
    A string is a semi-palindrome if:
    - It has length >= 2, AND
    - There exists a positive integer d such that 1 <= d < len and len % d == 0, AND
    - Taking indices that have the same modulo by d forms a palindrome.
    
    For example: "aa", "aba", "adbgad", and "abab" are semi-palindromes.
    "a", "ab", and "abca" are not semi-palindromes.
    
    Args:
        s: A string consisting only of lowercase English letters (2 <= len(s) <= 200)
        k: Number of substrings to partition into (1 <= k <= len(s) / 2)
    
    Returns:
        The minimum number of letter changes required to make all k substrings semi-palindromes.
    
    >>> min_changes_for_semi_palindromes("abcac", 2)
    1
    >>> min_changes_for_semi_palindromes("abcdef", 2)
    2
    >>> min_changes_for_semi_palindromes("aabbaa", 3)
    0
    """
    pass
