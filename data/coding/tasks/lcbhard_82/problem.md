# can_reach_end

Task `lcbhard_82`. Implement the function `can_reach_end` in `solution.py`.

## Signature

```python
from typing import List, Tuple


def can_reach_end(n: int, m: int, a: int, b: int, bad_intervals: List[Tuple[int, int]]) -> bool:
```

## Statement

Determine if it's possible to move from square 1 to square N by repeatedly jumping.

There are N squares arranged in a row, labeled 1 to N. You start at square 1 and want to reach square N.
In each move, if you're at square x, you can jump to square x + i where:
- a <= i <= b
- x + i <= n
- square x + i is not in any of the bad intervals

A square j is bad if there exists some interval (l_i, r_i) such that l_i <= j <= r_i.

Args:
    n: The number of squares (2 <= n <= 10^12)
    m: The number of bad intervals (0 <= m <= 2*10^4)
    a: The minimum jump size (1 <= a <= b <= 20)
    b: The maximum jump size
    bad_intervals: List of tuples (l_i, r_i) representing bad intervals.
                  The intervals are sorted and non-overlapping.

Returns:
    True if it's possible to reach square n from square 1, False otherwise.

>>> can_reach_end(24, 2, 3, 5, [(7, 8), (17, 20)])
True
>>> can_reach_end(30, 1, 5, 8, [(4, 24)])
False
>>> can_reach_end(100, 4, 10, 11, [(16, 18), (39, 42), (50, 55), (93, 99)])
True
