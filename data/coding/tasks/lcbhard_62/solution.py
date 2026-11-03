def count_paths(H: int, W: int, K: int, grid: list[str]) -> int:
    """ Count the number of ways to make K moves on a grid starting from any empty cell.
    
    The grid has H rows and W columns. Each cell is either empty ('.') or blocked ('#').
    A path consists of K+1 cells (starting cell + K moves) where:
    - All cells in the path are empty
    - Consecutive cells are adjacent (up, down, left, or right)
    - No cell is visited more than once in the path
    
    Args:
        H: Number of rows (1 <= H <= 10)
        W: Number of columns (1 <= W <= 10)
        K: Number of moves (1 <= K <= 11)
        grid: List of H strings, each of length W, representing the grid
    
    Returns:
        The number of valid paths of length K+1
    
    >>> count_paths(2, 2, 2, ['.#', '..'])
    2
    >>> count_paths(2, 3, 1, ['.#.', '#.#'])
    0
    """
    pass
