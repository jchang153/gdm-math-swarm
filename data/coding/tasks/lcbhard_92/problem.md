# sum_of_subarray_powers

Task `lcbhard_92`. Implement the function `sum_of_subarray_powers` in `solution.py`.

## Signature

```python
from typing import List


def sum_of_subarray_powers(n: int, k: int, a: List[int]) -> int:
```

## Statement

Given a list of n positive integers and a power k, compute the sum of all 
subarray sums raised to the power k, modulo 998244353.

For each subarray from index l to r (1-indexed in problem, 0-indexed here),
compute (a[l] + a[l+1] + ... + a[r])^k and sum all these values.

Args:
    n: The length of the array (1 <= n <= 2*10^5)
    k: The power to raise subarray sums to (1 <= k <= 10)
    a: List of integers (0 <= a[i] < 998244353)

Returns:
    The sum of all subarray sums raised to power k, modulo 998244353

>>> sum_of_subarray_powers(3, 2, [3, 1, 2])
75
>>> sum_of_subarray_powers(1, 10, [0])
0
>>> sum_of_subarray_powers(10, 5, [91, 59, 85, 60, 57, 72, 12, 3, 27, 16])
428633385
