# max_eleven_twentytwo_subsequence

Task `lcbhard_65`. Implement the function `max_eleven_twentytwo_subsequence` in `solution.py`.

## Signature

```python
def max_eleven_twentytwo_subsequence(s: str, l: int, r: int) -> int:
```

## Statement

Find the maximum length of a subsequence within s[l-1:r] that forms an 11/22 string.

An 11/22 string is defined as:
- It has odd length
- The first (len+1)//2 - 1 characters are all '1'
- The middle character at position (len+1)//2 is '/'
- The remaining characters are all '2'

For example: '/', '1/2', '11/22', '111/222' are 11/22 strings.

Args:
    s: A string consisting of characters '1', '2', and '/'
    l: Starting position (1-indexed)
    r: Ending position (1-indexed, inclusive)

Returns:
    The maximum length of a subsequence that forms an 11/22 string, or 0 if none exists.

>>> max_eleven_twentytwo_subsequence('111/212', 1, 7)
5
>>> max_eleven_twentytwo_subsequence('111/212/1122', 9, 12)
0
>>> max_eleven_twentytwo_subsequence('111/212/1122', 3, 6)
3
