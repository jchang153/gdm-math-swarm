# min_operations_to_match_grids

Task `lcbhard_42`. Implement the function `min_operations_to_match_grids` in `solution.py`.

## Signature

```python
from typing import List, Optional


def min_operations_to_match_grids(H: int, W: int, A: List[List[int]], B: List[List[int]]) -> Optional[int]:
```

## Statement

Given two grids A and B of dimensions H x W, determine if it's possible to transform 
grid A into grid B using only adjacent row swaps and adjacent column swaps.

Operations allowed:
- Swap row i with row i+1 (for 1 <= i < H)
- Swap column i with column i+1 (for 1 <= i < W)

If transformation is possible, return the minimum number of operations required.
If transformation is impossible, return None.

Args:
    H: Number of rows (2 <= H <= 5)
    W: Number of columns (2 <= W <= 5)
    A: Initial grid as a list of lists
    B: Target grid as a list of lists

Returns:
    Minimum number of operations if possible, None otherwise

>>> min_operations_to_match_grids(2, 2, [[2, 1], [1, 2]], [[1, 2], [2, 1]])
1
>>> min_operations_to_match_grids(2, 2, [[1, 1], [1, 1]], [[1, 1], [1, 1000000000]])
None
>>> min_operations_to_match_grids(3, 3, [[8, 1, 6], [3, 5, 7], [4, 9, 2]], [[8, 1, 6], [3, 5, 7], [4, 9, 2]])
0
