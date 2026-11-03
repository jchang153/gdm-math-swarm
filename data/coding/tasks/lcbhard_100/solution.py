def count_strongly_connected_partitions(n: int, s: str) -> int:
    """ Given a directed graph with 2n vertices numbered 1 to 2n, with initial edges from 
    vertex i to vertex i+1 (for i = 1 to 2n-1), and a string s of length 2n consisting 
    of n 'W's and n 'B's indicating vertex colors (W for white, B for black), count the 
    number of ways to partition the vertices into n pairs (each with one white and one 
    black vertex) such that after adding a directed edge from the white to black vertex 
    in each pair, the resulting graph is strongly connected.
    
    Return the count modulo 998244353.
    
    A directed graph is strongly connected if it's possible to travel from any vertex 
    to any other vertex by following edges.
    
    >>> count_strongly_connected_partitions(2, 'BWBW')
    1
    >>> count_strongly_connected_partitions(4, 'BWWBWBWB')
    0
    >>> count_strongly_connected_partitions(9, 'BWWBWBBBWWBWBBWWBW')
    240792
    """
    pass
