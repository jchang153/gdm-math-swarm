from typing import List


def max_knight_moves(kx: int, ky: int, positions: List[List[int]]) -> int:
    """ 
    On a 50x50 chessboard, there is a knight at position (kx, ky) and pawns at the given positions.
    Alice and Bob play a turn-based game (Alice goes first) where each player:
    - Selects any remaining pawn and captures it with the knight using the minimum number of moves
    - The knight can pass other pawns without capturing them during the move
    
    Alice wants to maximize the total number of moves, while Bob wants to minimize them.
    Both players play optimally.
    
    A knight moves in an L-shape: 2 cells in one direction and 1 cell perpendicular to that.
    
    Args:
        kx: Knight's initial x-coordinate (0 <= kx <= 49)
        ky: Knight's initial y-coordinate (0 <= ky <= 49)
        positions: List of [x, y] coordinates of pawns (1 <= len(positions) <= 15)
    
    Returns:
        The maximum total number of moves Alice can achieve
    
    >>> max_knight_moves(1, 1, [[0, 0]])
    4
    >>> max_knight_moves(0, 2, [[1, 1], [2, 2], [3, 3]])
    8
    >>> max_knight_moves(0, 0, [[1, 2], [2, 4]])
    3
    """
    pass
