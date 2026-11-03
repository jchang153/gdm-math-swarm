from typing import Optional, List


def solve_grid(n: int, r: str, c: str) -> Optional[List[str]]:
    """ Given an integer N and strings R and C of length N consisting of 'A', 'B', and 'C',
    determine if it's possible to fill an N×N grid such that:
    - Each row and column contains exactly one 'A', one 'B', and one 'C'
    - The leftmost character written in the i-th row matches the i-th character of R
    - The topmost character written in the i-th column matches the i-th character of C
    
    Empty cells are represented by '.' in the output.
    
    Args:
        n: The size of the grid (3 <= n <= 5)
        r: String of length n specifying leftmost characters in each row
        c: String of length n specifying topmost characters in each column
    
    Returns:
        A list of n strings representing the filled grid if possible, None otherwise.
        Each string has length n, where '.' represents empty cells.
    
    >>> solve_grid(3, 'ABC', 'ABC')
    ['ABC', 'BCA', 'CAB']
    >>> solve_grid(3, 'AAA', 'BBB')
    None
    """
    pass
