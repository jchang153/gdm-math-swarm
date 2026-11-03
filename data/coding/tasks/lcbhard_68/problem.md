# sum_absolute_differences

Task `lcbhard_68`. Implement the function `sum_absolute_differences` in `solution.py`.

## Signature

```python
from typing import List, Tuple


def sum_absolute_differences(n: int, a: List[int], b: List[int], queries: List[Tuple[int, int]]) -> List[int]:
```

## Statement

Given two arrays A and B of length n, and a list of queries where each query is a tuple (x, y),
compute for each query the sum of |A[i] - B[j]| for all i from 0 to x-1 and j from 0 to y-1.

Args:
    n: The length of arrays A and B (1 <= n <= 10^5)
    a: Array A with elements (0 <= A[i] <= 2*10^8)
    b: Array B with elements (0 <= B[j] <= 2*10^8)
    queries: List of tuples (x, y) where 1 <= x, y <= n

Returns:
    A list of integers where the k-th element is the sum for the k-th query

>>> sum_absolute_differences(2, [2, 4], [3, 5], [(1, 1), (1, 2), (2, 1), (2, 2)])
[1, 4, 2, 6]
>>> sum_absolute_differences(5, [1163686, 28892, 1263085, 2347878, 520306], [1332157, 1202905, 2437161, 1291976, 563395], [(5, 3), (1, 5), (2, 3), (1, 2), (5, 5)])
[13331322, 2209746, 6366712, 207690, 20241215]
