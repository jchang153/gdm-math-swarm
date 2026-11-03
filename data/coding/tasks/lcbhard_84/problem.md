# count_balanced_graphs

Task `lcbhard_84`. Implement the function `count_balanced_graphs` in `solution.py`.

## Signature

```python
def count_balanced_graphs(n: int, p: int) -> list[int]:
```

## Statement

Given an even positive integer n and a prime number p, for each M from n-1 to n*(n-1)//2,
count the number of undirected connected simple graphs with n vertices (labeled 1 to n) and M edges
such that the number of vertices whose shortest distance from vertex 1 is even equals the number
of vertices whose shortest distance from vertex 1 is odd. Return these counts modulo p.

Args:
    n: Even positive integer (2 <= n <= 30)
    p: Prime number (10^8 <= p <= 10^9)

Returns:
    List of counts modulo p for M = n-1, n, n+1, ..., n*(n-1)//2

>>> count_balanced_graphs(4, 998244353)
[12, 9, 3, 0]
>>> count_balanced_graphs(6, 924844033)
[810, 2100, 3060, 3030, 2230, 1210, 450, 100, 10, 0, 0]
