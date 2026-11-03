# min_cost_to_achieve_goal

Task `lcbhard_39`. Implement the function `min_cost_to_achieve_goal` in `solution.py`.

## Signature

```python
from typing import List, Tuple


def min_cost_to_achieve_goal(n: int, k: int, p: int, plans: List[Tuple[int, List[int]]]) -> int:
```

## Statement

Given N development plans for a product with K parameters, find the minimum cost to raise
all parameters to at least P. Each plan has a cost and increases parameters by specific amounts.
Plans cannot be executed more than once.

Args:
    n: Number of development plans (1 <= n <= 100)
    k: Number of parameters (1 <= k <= 5)
    p: Target value for all parameters (1 <= p <= 5)
    plans: List of tuples where each tuple contains:
           - cost: Cost of the plan (1 <= cost <= 10^9)
           - increases: List of K integers representing parameter increases (0 <= increase <= p)

Returns:
    The minimum total cost to achieve the goal, or -1 if impossible.

>>> min_cost_to_achieve_goal(4, 3, 5, [(5, [3, 0, 2]), (3, [1, 2, 3]), (3, [2, 4, 0]), (1, [0, 1, 4])])
9
>>> min_cost_to_achieve_goal(7, 3, 5, [(85, [1, 0, 1]), (37, [1, 1, 0]), (38, [2, 0, 0]), (45, [0, 2, 2]), (67, [1, 1, 0]), (12, [2, 2, 0]), (94, [2, 2, 1])])
-1
