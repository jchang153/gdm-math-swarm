# count_paths

Task `lcbhard_93`. Implement the function `count_paths` in `solution.py`.

## Signature

```python
def count_paths(W: int, H: int, L: int, R: int, D: int, U: int) -> int:
```

## Statement

Count the number of possible paths in a 2D grid with specific constraints.

A block exists at lattice point (x, y) if:
- 0 <= x <= W and 0 <= y <= H
- AND (x < L OR x > R OR y < D OR y > U)

A path starts at any block and can move one unit in the positive x-direction 
or positive y-direction any number of times (including zero), but must always 
land on a block.

Return the total number of possible paths modulo 998244353.

Args:
    W: Maximum x-coordinate (0 <= L <= R <= W <= 10^6)
    H: Maximum y-coordinate (0 <= D <= U <= H <= 10^6)
    L, R: x-coordinate bounds for the "hole" in the grid
    D, U: y-coordinate bounds for the "hole" in the grid

Returns:
    Number of possible paths modulo 998244353

>>> count_paths(4, 3, 1, 2, 2, 3)
192
>>> count_paths(10, 12, 4, 6, 8, 11)
4519189
>>> count_paths(192, 25, 0, 2, 0, 9)
675935675
