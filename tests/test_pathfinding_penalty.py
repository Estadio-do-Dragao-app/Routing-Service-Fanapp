import pytest
from pathFinding import PathFinder

def test_waittime_penalty_impact():
    # Setup a simple graph: START -> A -> POI -> B -> END
    # Alternative: START -> A -> C -> B -> END
    # Realistic GPS coordinates (Porto, Portugal approx)
    # 0.0001 degrees latitude ~= 11.1 meters
    # 0.0001 degrees longitude ~= 8.3 meters at 41 deg N
    
    map_data = {
        "nodes": [
            {"id": "START", "x": -8.6290, "y": 41.1490, "level": 0},
            {"id": "A", "x": -8.6291, "y": 41.1490, "level": 0},
            {"id": "POI", "x": -8.6300, "y": 41.1490, "level": 0, "type": "poi"},
            {"id": "B", "x": -8.6309, "y": 41.1490, "level": 0},
            {"id": "C", "x": -8.6300, "y": 41.1500, "level": 0},
            {"id": "END", "x": -8.6310, "y": 41.1490, "level": 0}
        ],
        "edges": [
            {"from": "START", "to": "A", "w": 10},
            {"from": "A", "to": "POI", "w": 100},
            {"from": "POI", "to": "B", "w": 100},
            {"from": "B", "to": "END", "w": 10},
            {"from": "A", "to": "C", "w": 141},
            {"from": "C", "to": "B", "w": 141}
        ]
    }
    
    pf = PathFinder(map_data)
    
    # 1. No wait time: Path through POI is shorter (10+100+100+10 = 220)
    # vs path through C (10+141+141+10 = 302)
    path, _ = pf.find_path("START", "END", {}, waittime_data={})
    assert "POI" in path
    assert "C" not in path
    
    # 2. High wait time at POI (10 minutes)
    # Penalty = 10 * 60 * 1.4 * 2.0 (multiplier for passing through) = 1680
    # New cost through POI = 220 + 1680 = 1900
    # Path through C remains 302. Should switch to C.
    path_busy, cost_busy = pf.find_path("START", "END", {}, waittime_data={"POI": 10})
    assert "POI" not in path_busy
    assert "C" in path_busy
    assert cost_busy == pytest.approx(302.0)

def test_poi_destination_penalty():
    # If POI is the destination, penalty should be lower (no 2.0x multiplier)
    map_data = {
        "nodes": [
            {"id": "START", "x": -8.6290, "y": 41.1490, "level": 0},
            {"id": "POI", "x": -8.6300, "y": 41.1490, "level": 0, "type": "poi"}
        ],
        "edges": [
            {"from": "START", "to": "POI", "w": 100}
        ]
    }
    pf = PathFinder(map_data)
    
    # Wait time 1 min = 1 * 60 * 1.4 = 84 penalty
    # Total cost = 100 (dist) + 84 (penalty) = 184
    path, cost = pf.find_path("START", "POI", {}, waittime_data={"POI": 1})
    assert cost == pytest.approx(184.0)

if __name__ == "__main__":
    pytest.main([__file__])
