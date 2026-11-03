# count_valid_strings

Task `lcbhard_10`. Implement the function `count_valid_strings` in `solution.py`.

## Signature

```python
from typing import List


def count_valid_strings(K: int, C: List[int]) -> int:
```

## Statement

Count the number of strings consisting of uppercase English letters with length between 
1 and K (inclusive) that satisfy the following condition: for each letter (A=0, B=1, ..., Z=25),
the number of occurrences in the string is at most C[i].

Return the count modulo 998244353.

Args:
    K: Maximum length of strings to consider (1 <= K <= 1000)
    C: List of 26 integers where C[i] is the maximum allowed occurrences of the i-th letter
        (0 <= C[i] <= 1000)

Returns:
    The number of valid strings modulo 998244353

>>> count_valid_strings(2, [2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
10
>>> count_valid_strings(358, [1, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
64
