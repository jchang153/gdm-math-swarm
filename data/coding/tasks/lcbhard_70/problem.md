# can_transform

Task `lcbhard_70`. Implement the function `can_transform` in `solution.py`.

## Signature

```python
def can_transform(k: int, s: str, t: str) -> bool:
```

## Statement

Determine if string s can be transformed into string t using at most k operations.

Available operations:
- Insert any character at any position in the string
- Delete any character from the string
- Replace any character with another character

Args:
    k: Maximum number of operations allowed (1 <= k <= 20)
    s: Source string (1 <= len(s) <= 500000)
    t: Target string (1 <= len(t) <= 500000)

Returns:
    True if s can be transformed into t with at most k operations, False otherwise

>>> can_transform(3, 'abc', 'awtf')
True
>>> can_transform(2, 'abc', 'awtf')
False
>>> can_transform(17, 'twothousandtwentyfour', 'happynewyear')
True
