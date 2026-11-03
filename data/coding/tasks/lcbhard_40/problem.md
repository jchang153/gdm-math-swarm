# min_sensor_cost

Task `lcbhard_40`. Implement the function `min_sensor_cost` in `solution.py`.

## Signature

```python
from typing import List, Tuple


def min_sensor_cost(n: int, sections: List[int], sensor1: Tuple[int, int, int], sensor2: Tuple[int, int, int]) -> int:
```

## Statement

Given N sections to monitor on a conveyor belt, find the minimum cost to monitor all sections using two types of sensors.

Each section has a length in meters that needs to be monitored. There are two types of sensors available:
- Type 1: Can monitor L1 meters, costs C1 per sensor, and you can use at most K1 sensors
- Type 2: Can monitor L2 meters, costs C2 per sensor, and you can use at most K2 sensors

Sensors can overlap and can monitor more than the required length. For example, to monitor a 5-meter section,
you could use one 4-meter sensor and one 2-meter sensor (total coverage: 6 meters).

Args:
    n: Number of sections to monitor (1 <= n <= 100)
    sections: List of section lengths D_i in meters (1 <= D_i <= 10^5)
    sensor1: Tuple (L1, C1, K1) - length, cost, and max count for type 1 sensors
    sensor2: Tuple (L2, C2, K2) - length, cost, and max count for type 2 sensors
    
Returns:
    The minimum total cost to monitor all sections, or -1 if impossible.
    
>>> min_sensor_cost(3, [3, 5, 10], (4, 3, 3), (2, 2, 6))
17
>>> min_sensor_cost(3, [3, 5, 10], (4, 3, 3), (2, 2, 3))
-1
>>> min_sensor_cost(2, [4, 8], (3, 1, 100), (4, 10000, 100))
5
