# count_good_string_replacements

Task `lcbhard_76`. Implement the function `count_good_string_replacements` in `solution.py`.

## Signature

```python
def count_good_string_replacements(n: int, k: int, s: str) -> int:
```

## Statement

Given a string s of length n consisting of 'A', 'B', 'C', and '?', count the number
of ways to replace each '?' with 'A', 'B', or 'C' such that the resulting string contains
at least k "good strings" as contiguous substrings.

A "good string" is a non-empty string that can be turned into an empty string by:
- Operation 1: Choose two identical characters and delete them
- Operation 2: Choose one A, one B, and one C and delete them

Substrings at different positions are counted separately even if they are identical.
Return the count modulo 998244353.

Args:
    n: Length of the string (1 <= n <= 50)
    k: Minimum number of good substrings required (0 <= k <= n*(n+1)/2)
    s: String of length n consisting of 'A', 'B', 'C', and '?'

Returns:
    Number of ways to replace '?' characters modulo 998244353

>>> count_good_string_replacements(4, 2, 'A?AB')
1
>>> count_good_string_replacements(1, 0, 'A')
1
>>> count_good_string_replacements(2, 1, 'CC')
1
