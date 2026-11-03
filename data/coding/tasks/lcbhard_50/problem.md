# count_good_strings

Task `lcbhard_50`. Implement the function `count_good_strings` in `solution.py`.

## Signature

```python
def count_good_strings(n: int, k: int, s: str) -> int:
```

## Statement

Given a string s of length n consisting of characters 'A', 'B', and '?',
and a positive integer k, count the number of ways to replace each '?' with
either 'A' or 'B' such that the resulting string has no palindromic substring
of length k.

A string is considered "good" if no contiguous substring of length k is a palindrome.
Return the count modulo 998244353.

Args:
    n: The length of the string (2 <= k <= n <= 1000)
    k: The length of substrings to check for palindromes (k <= 10)
    s: A string of length n consisting of 'A', 'B', and '?'

Returns:
    The number of good strings modulo 998244353

>>> count_good_strings(7, 4, 'AB?A?BA')
1
>>> count_good_strings(40, 7, '????????????????????????????????????????')
116295436
>>> count_good_strings(15, 5, 'ABABA??????????')
0
