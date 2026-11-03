# find_cube_difference_pair

Task `lcbhard_26`. Implement the function `find_cube_difference_pair` in `solution.py`.

## Signature

```python
from typing import Optional, Tuple


def find_cube_difference_pair(n: int) -> Optional[Tuple[int, int]]:
```

## Statement

Find a pair of positive integers (x, y) such that x^3 - y^3 = n.

Args:
    n: A positive integer (1 <= n <= 10^18)

Returns:
    A tuple (x, y) of positive integers such that x^3 - y^3 = n if such a pair exists,
    None otherwise.

>>> find_cube_difference_pair(397)
(12, 11)
>>> find_cube_difference_pair(1)
None
>>> find_cube_difference_pair(7)
(2, 1)
