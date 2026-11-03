# shortest_palindrome_with_prefix

Task `lcbhard_91`. Implement the function `shortest_palindrome_with_prefix` in `solution.py`.

## Signature

```python
def shortest_palindrome_with_prefix(s: str) -> str:
```

## Statement

Find the shortest palindrome that has s as its prefix.

Args:
    s: A string consisting of uppercase English letters (length 1 to 500000)
    
Returns:
    The shortest palindrome that starts with s
    
>>> shortest_palindrome_with_prefix('ABC')
'ABCBA'
>>> shortest_palindrome_with_prefix('Z')
'Z'
>>> shortest_palindrome_with_prefix('TREE')
'TREERT'
