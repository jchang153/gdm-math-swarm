# calculate_black_area_doubled

Task `lcbhard_7`. Implement the function `calculate_black_area_doubled` in `solution.py`.

## Signature

```python
def calculate_black_area_doubled(A: int, B: int, C: int, D: int) -> int:
```

## Statement

Calculate twice the area of black-painted regions inside a rectangle on AtCoder's wallpaper pattern.

The wallpaper pattern is defined as follows:
- The plane is divided by lines: x = n (integer n), y = n (even n), and x + y = n (even n)
- Adjacent regions along these lines have different colors (black or white)
- The region containing (0.5, 0.5) is black

Given a rectangle with bottom-left vertex at (A, B) and top-right vertex at (C, D),
calculate the area of black regions inside it and return twice that area.

Args:
    A, B: Coordinates of the bottom-left vertex
    C, D: Coordinates of the top-right vertex
    
Constraints:
    -10^9 <= A, B, C, D <= 10^9
    A < C and B < D
    
Returns:
    Twice the area of black regions (guaranteed to be an integer)

>>> calculate_black_area_doubled(0, 0, 3, 3)
10
>>> calculate_black_area_doubled(-1, -2, 1, 3)
11
>>> calculate_black_area_doubled(-1000000000, -1000000000, 1000000000, 1000000000)
4000000000000000000
