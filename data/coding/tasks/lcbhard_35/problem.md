# min_time_to_reduce_sum

Task `lcbhard_35`. Implement the function `min_time_to_reduce_sum` in `solution.py`.

## Signature

```python
from typing import List


def min_time_to_reduce_sum(nums1: List[int], nums2: List[int], x: int) -> int:
```

## Statement

Given two 0-indexed integer arrays nums1 and nums2 of equal length, find the minimum time
to make the sum of nums1 less than or equal to x.

Every second:
1. All elements nums1[i] are incremented by nums2[i]
2. You can choose one index i and set nums1[i] = 0

Return the minimum time needed, or -1 if impossible.

>>> min_time_to_reduce_sum([1, 2, 3], [1, 2, 3], 4)
3
>>> min_time_to_reduce_sum([1, 2, 3], [3, 3, 3], 4)
-1
