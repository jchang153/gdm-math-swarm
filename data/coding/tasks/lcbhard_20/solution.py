def min_tile_moves(K: int, S_x: int, S_y: int, T_x: int, T_y: int) -> int:
    """ Find the minimum number of moves between tiles on a coordinate plane.
    
    Tiles are placed according to these rules:
    - For integer triple (i, j, k) with 0 <= k < K:
      - If i and j have same parity: tile covers iK <= x <= (i+1)K and jK+k <= y <= jK+k+1
      - If i and j have different parity: tile covers iK+k <= x <= iK+k+1 and jK <= y <= (j+1)K
    
    Two tiles are adjacent if their edges share a positive-length segment.
    
    Args:
        K: The tile parameter (2 <= K <= 10^16)
        S_x, S_y: Starting coordinates (tile containing (S_x + 0.5, S_y + 0.5))
        T_x, T_y: Target coordinates (tile containing (T_x + 0.5, T_y + 0.5))
    
    Returns:
        Minimum number of moves to adjacent tiles needed to go from start to target tile.
    
    >>> min_tile_moves(3, -2, 1, 4, -1)
    4
    >>> min_tile_moves(4, 8, 8, 0, 2)
    4
    >>> min_tile_moves(5, -1000000000000, -1000000000000, 1000000000000, 1000000000000)
    800000000000
    """
    pass
