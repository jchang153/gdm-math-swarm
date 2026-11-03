from typing import List


def minimum_shipping_dissatisfaction(n: int, k: int, x: int, order_days: List[int]) -> int:
    """ Given n orders placed on specific days, find the minimum total dissatisfaction when shipping them.
    
    Rules:
    - At most k orders can be shipped together in one batch
    - Order i (placed on day order_days[i]) can only be shipped on or after that day
    - After shipping on day a, the next shipment can only be on day a+x or later
    - Dissatisfaction for order i shipped on day s is (s - order_days[i])
    
    Args:
        n: Number of orders (1 <= n <= 100)
        k: Maximum orders per shipment (1 <= k <= n)
        x: Days between shipments (1 <= x <= 10^9)
        order_days: List of n order placement days in non-decreasing order (1 <= order_days[i] <= 10^12)
    
    Returns:
        The minimum possible total dissatisfaction
    
    >>> minimum_shipping_dissatisfaction(5, 2, 3, [1, 5, 6, 10, 12])
    2
    >>> minimum_shipping_dissatisfaction(1, 1, 1000000000, [1000000000000])
    0
    """
    pass
