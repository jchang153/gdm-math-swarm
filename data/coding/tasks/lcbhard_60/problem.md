# max_production_capacity

Task `lcbhard_60`. Implement the function `max_production_capacity` in `solution.py`.

## Signature

```python
from typing import List, Tuple


def max_production_capacity(n: int, x: int, machines: List[Tuple[int, int, int, int]]) -> int:
```

## Statement

Given N processes and a budget X, find the maximum achievable production capacity.

For each process i, there are two types of machines available:
- Machine type S: processes A_i products per day, costs P_i yen per unit
- Machine type T: processes B_i products per day, costs Q_i yen per unit

You can purchase any number of each machine type (including zero).
The production capacity is defined as the minimum processing capability across all processes.

Args:
    n: Number of processes (1 <= n <= 100)
    x: Total budget in yen (1 <= x <= 10^7)
    machines: List of n tuples, each containing (A_i, P_i, B_i, Q_i) where:
              - A_i, B_i: products per day (1 <= A_i, B_i <= 100)
              - P_i, Q_i: cost per unit (1 <= P_i, Q_i <= 10^7)

Returns:
    Maximum achievable production capacity (minimum products/day across all processes)

>>> max_production_capacity(3, 22, [(2, 5, 3, 6), (1, 1, 3, 3), (1, 3, 2, 4)])
4
>>> max_production_capacity(1, 10000000, [(100, 1, 100, 1)])
1000000000
>>> max_production_capacity(1, 1, [(1, 10000000, 1, 10000000)])
0
