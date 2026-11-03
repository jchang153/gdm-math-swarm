# min_absolute_difference

Task `lcbhard_15`. Implement the function `min_absolute_difference` in `solution.py`.

## Signature

```python
from typing import List


def min_absolute_difference(nums: List[int], k: int) -> int:
```

## Statement

Given an array nums and an integer k, find a subarray such that the absolute difference 
between k and the bitwise OR of the subarray elements is minimized.

A subarray is a contiguous non-empty sequence of elements within an array.

Args:
    nums: List of positive integers (1 <= nums[i] <= 10^9)
    k: Target value (1 <= k <= 10^9)

Returns:
    The minimum possible value of |k - (bitwise OR of subarray)|

>>> min_absolute_difference([1, 2, 4, 5], 3)
0
>>> min_absolute_difference([1, 3, 1, 3], 2)
1
>>> min_absolute_difference([1], 10)
9
