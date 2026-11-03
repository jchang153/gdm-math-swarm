from typing import List


def findProductQueries(queries: List[List[int]]) -> List[int]:
    """ A powerful array for an integer x is the shortest sorted array of powers of two that sum up to x.
    For example, the powerful array for 11 is [1, 2, 8].
    
    The array big_nums is created by concatenating the powerful arrays for every positive integer i 
    in ascending order: 1, 2, 3, and so forth. Thus, big_nums starts as [1, 2, 1, 2, 4, 1, 4, 2, 4, 1, 2, 4, 8, ...].
    
    Given a list of queries where each query is [from_i, to_i, mod_i], compute 
    (big_nums[from_i] * big_nums[from_i + 1] * ... * big_nums[to_i]) % mod_i for each query.
    
    Note: big_nums uses 0-based indexing.
    
    Args:
        queries: List of queries, where each query is [from_i, to_i, mod_i]
                 0 <= from_i <= to_i <= 10^15, 1 <= mod_i <= 10^5
    
    Returns:
        List of integers where the i-th element is the answer to the i-th query
    
    >>> findProductQueries([[1,3,7]])
    [4]
    >>> findProductQueries([[2,5,3],[7,7,4]])
    [2, 2]
    """
    pass
