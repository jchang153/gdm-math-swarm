# max_score

Task `lcbhard_55`. Implement the function `max_score` in `solution.py`.

## Signature

```python
from typing import List


def max_score(grid: List[List[int]]) -> int:
```

## Statement

Given a 2D matrix of positive integers, select one or more cells such that:
- No two selected cells are in the same row
- The values in the selected cells are unique

Return the maximum sum of values you can achieve.

Args:
    grid: A 2D list where 1 <= len(grid), len(grid[i]) <= 10 and 1 <= grid[i][j] <= 100

>>> max_score([[1,2,3],[4,3,2],[1,1,1]])
8
>>> max_score([[8,7,6],[8,3,2]])
15
