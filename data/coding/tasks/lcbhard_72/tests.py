def check(candidate):
    # Test case 1
    assert candidate(5, 16, [1, 8, 5, 10, 13]) == (1, 3, 4)
    
    # Test case 2
    assert candidate(5, 20, [1, 8, 5, 10, 13]) is None
    
    # Test case 3
    assert candidate(10, 100000, [73766, 47718, 74148, 49218, 76721, 31902, 21994, 18880, 29598, 98917]) == (4, 6, 8)
    
    # Test case 4
    assert candidate(10, 48, [11, 28, 16, 6, 34, 1, 46, 1, 15, 2]) == (6, 7, 8)
    
    # Test case 5
    assert candidate(3, 5, [1, 2, 3]) is None
    
    # Test case 6
    assert candidate(5, 12, [11, 11, 10, 12, 11]) is None
    
    # Test case 7
    assert candidate(7, 4, [2, 3, 4, 2, 2, 2, 4]) is None
    
    # Test case 8
    assert candidate(3, 6, [2, 2, 2]) == (1, 2, 3)
    
    # Test case 9
    assert candidate(7, 6, [1, 2, 3, 4, 4, 4, 4]) == (1, 2, 3)
    
    # Test case 10
    assert candidate(5, 2, [2, 2, 2, 2, 2]) is None
    
    assert candidate(5, 16, [1, 8, 5, 10, 13]) is None
    
    # Test case 11
    assert candidate(6, 3, [1, 1, 1, 1, 1, 1]) == (1, 2, 3)
    
    # Test case 12
    result = candidate(10, 21, [21, 19, 20, 19, 20, 21, 19, 21, 19, 19])
    assert result is None
    
    # Test case 13
    assert candidate(3, 13, [12, 11, 12]) is None
    
    # Test case 14
    assert candidate(5, 21, [15, 1, 19, 14, 1]) == (2, 3, 5)
    
    # Test case 15
    assert candidate(6, 32, [1, 17, 1, 30, 12, 10]) == (1, 3, 4)
    
    # Test case 16
    assert candidate(3, 44, [1, 1, 42]) == (1, 2, 3)
    
    # Test case 17
    assert candidate(5, 29, [2, 1, 27, 1, 25]) == (2, 3, 4)
    
    # Test case 18
    assert candidate(8, 15, [13, 12, 10, 11, 14, 1, 1, 13]) == (1, 6, 7)
    
    # Test case 19
    assert candidate(7, 4, [3, 1, 2, 4, 2, 1, 1]) == (2, 3, 6)
    
    # Test case 20
    assert candidate(7, 16, [15, 16, 16, 15, 16, 14, 15]) is None
