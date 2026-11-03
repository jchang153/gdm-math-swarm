# card_game_winner

Task `lcbhard_46`. Implement the function `card_game_winner` in `solution.py`.

## Signature

```python
from typing import List, Tuple


def card_game_winner(n: int, cards: List[Tuple[int, int]]) -> str:
```

## Statement

Two players are playing a card game. Each card has a front number and a back number.
Players take turns removing pairs of cards where either the front numbers match or the back numbers match.
The player who cannot make a move loses.

Takahashi goes first. Both players play optimally.

Args:
    n: Number of cards (1 <= n <= 18)
    cards: List of tuples where cards[i] = (A_i, B_i) represents the front and back numbers of card i

Returns:
    "Takahashi" if Takahashi wins, "Aoki" if Aoki wins

>>> card_game_winner(5, [(1, 9), (2, 5), (4, 9), (1, 4), (2, 5)])
'Aoki'
>>> card_game_winner(9, [(3, 2), (1, 7), (4, 1), (1, 8), (5, 2), (9, 8), (2, 1), (6, 8), (5, 2)])
'Takahashi'
