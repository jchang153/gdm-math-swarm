def min_grid_sum(H: int, W: int, grid: list[str]) -> int:
    """ Given a H x W grid where each cell contains 0 or 1, find the minimum possible sum of all cells
    after performing any number of row and column flip operations.
    
    Operations:
    - Row flip: Choose a row and flip all values in that row (0 becomes 1, 1 becomes 0)
    - Column flip: Choose a column and flip all values in that column (0 becomes 1, 1 becomes 0)
    
    Args:
        H: Number of rows (1 <= H <= 2*10^5)
        W: Number of columns (1 <= W <= 18)
        grid: List of H strings, each of length W consisting of '0' and '1'
    
    Returns:
        The minimum possible sum of all cells after performing operations
    
    >>> min_grid_sum(3, 3, ['100', '010', '110'])
    2
    >>> min_grid_sum(3, 4, ['1111', '1111', '1111'])
    0
    >>> min_grid_sum(1, 1, ['1'])
    0
    """
    pass
