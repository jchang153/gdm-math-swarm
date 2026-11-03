def count_polish_sequences(n: int, a: list[int]) -> int:
    """ Count the number of Polish sequences of length n that are lexicographically 
    not greater than the given sequence a, modulo 998244353.
    
    A sequence (V₁, V₂, ..., Vₘ) is Polish if there exist V₁ Polish sequences 
    W₁, W₂, ..., W_{V₁} such that the concatenation of (V₁), W₁, W₂, ..., W_{V₁} 
    in this order equals (V₁, V₂, ..., Vₘ). The sequence (0) is Polish by definition.
    
    Args:
        n: The length of sequences to consider (1 ≤ n ≤ 3×10⁵)
        a: The upper bound sequence of length n (0 ≤ a[i] < n)
    
    Returns:
        The count of Polish sequences lexicographically ≤ a, modulo 998244353
    
    >>> count_polish_sequences(6, [1, 1, 1, 2, 0, 0])
    2
    >>> count_polish_sequences(1, [0])
    1
    >>> count_polish_sequences(4, [1, 1, 0, 0])
    0
    """
    pass
