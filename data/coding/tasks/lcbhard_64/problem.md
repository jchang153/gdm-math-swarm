# card_game_winner

Task `lcbhard_64`. Implement the function `card_game_winner` in `solution.py`.

## Signature

```python
from typing import List, Tuple

def card_game_winner(n: int, m: int, l: int, takahashi_cards: List[int], aoki_cards: List[int], table_cards: List[int]) -> str:
```

## Statement

Takahashi and Aoki play a card game. Initially, Takahashi has n cards, Aoki has m cards, 
and there are l cards on the table. Starting with Takahashi, they take turns:
- Play one card from hand to table
- Optionally take one card from table that has a smaller number than the played card

The player who cannot make a move loses. Both players play optimally.

Args:
    n: Number of cards in Takahashi's hand
    m: Number of cards in Aoki's hand  
    l: Number of cards on the table
    takahashi_cards: List of card values in Takahashi's hand
    aoki_cards: List of card values in Aoki's hand
    table_cards: List of card values on the table
    
Returns:
    "Takahashi" if Takahashi wins, "Aoki" if Aoki wins
    
>>> card_game_winner(1, 1, 2, [2], [4], [1, 3])
'Aoki'
>>> card_game_winner(1, 1, 1, [11], [5], [10])
'Takahashi'
