import pytest
from unittest.mock import AsyncMock, patch
import httpx
from pathFinding import PathFinder

def test_pathfinder_initialization_varieties():
    map_data = {
        "nodes": [
            {"id": "N1", "x": 0.0, "y": 0.0, "level": 0},  # skipped because x=0, y=0
            {"id": "N2", "x": 1.0, "y": 1.0, "level": 0},
            {"id": "N3", "x": 1.0001, "y": 1.0001, "level": 0},
            {"id": "N4", "x": 2.0, "y": 2.0, "level": 0}
        ],
        "edges": [
            {"id": "E1", "from": "N2", "to": "N3", "w": 10},
            {"id": "E2", "from": "N3", "to": "N4", "w": 20},
            {"id": "E3", "from": "", "to": "N4", "w": 30}  # skipped invalid source
        ],
        "closures": [
            {"id": "E2"}  # will block E2
        ]
    }
    pf = PathFinder(map_data)
    assert "N1" not in pf.nodes
    assert "N2" in pf.nodes
    # E2 is skipped due to static closures, so graph from N3 to N4 won't be connected
    assert "N4" not in pf.graph

def test_bridge_disconnected_components():
    map_data = {
        "nodes": [
            {"id": "A1", "x": 1.0, "y": 1.0, "level": 0},
            {"id": "A2", "x": 1.0001, "y": 1.0001, "level": 0},
            {"id": "B1", "x": 1.0005, "y": 1.0005, "level": 0},
            {"id": "B2", "x": 1.0006, "y": 1.0006, "level": 0}
        ],
        "edges": [
            {"id": "E1", "from": "A1", "to": "A2", "w": 10},
            {"id": "E2", "from": "B1", "to": "B2", "w": 10}
        ]
    }
    pf = PathFinder(map_data)
    # The two islands (A1-A2 and B1-B2) are disconnected, but should be bridged because dist is ~60m (<100m)
    assert len(pf.graph.get("B1", [])) > 1

def test_find_nearest_node_fallback():
    map_data = {
        "nodes": [
            {"id": "A1", "x": 1.0, "y": 1.0, "level": 0}
        ],
        "edges": []
    }
    pf = PathFinder(map_data)
    # Search far away
    node = pf.find_nearest_node(10.0, 10.0, 0)
    assert node == "A1"

@pytest.mark.asyncio
async def test_fetch_map_data(monkeypatch):
    mock_client = AsyncMock()
    req = httpx.Request("GET", "https://fake/map")
    mock_client.get.return_value = httpx.Response(200, json={"nodes": [], "edges": []}, request=req)
    
    with patch.dict("os.environ", {"API_KEY": "test_key"}):
        res = await PathFinder.fetch_map_data(mock_client, "https://fake")
        assert res == {"nodes": [], "edges": []}

def test_pathfinding_variety():
    map_data = {
        "nodes": [
            {"id": "START", "x": 1.0, "y": 1.0, "level": 0},
            {"id": "STAIR1", "x": 1.0001, "y": 1.0001, "level": 0, "type": "stairs"},
            {"id": "STAIR2", "x": 1.0002, "y": 1.0002, "level": 1, "type": "stairs"},
            {"id": "SEAT", "x": 1.0003, "y": 1.0003, "level": 1, "type": "seat"},
            {"id": "END", "x": 1.0004, "y": 1.0004, "level": 1}
        ],
        "edges": [
            {"from": "START", "to": "STAIR1", "w": 10},
            {"from": "STAIR1", "to": "STAIR2", "w": 10},
            {"from": "STAIR2", "to": "SEAT", "w": 10},
            {"from": "SEAT", "to": "END", "w": 10}
        ]
    }
    pf = PathFinder(map_data)
    
    # Avoid stairs: should return no path since path must go STAIR1 -> STAIR2
    path, cost = pf.find_path("START", "END", {}, avoid_stairs=True)
    assert path == []
    assert cost == float("inf")
    
    # Seat node check: SEAT cannot be traversed if it's not the destination, so going START -> END fails
    path_normal, _ = pf.find_path("START", "END", {})
    assert "SEAT" not in path_normal
    
    # Dynamic blocked nodes
    path_blocked, _ = pf.find_path("START", "STAIR2", {}, blocked_nodes={"STAIR1"})
    assert path_blocked == []
    
    # Hazards (congestion > 2.0)
    congestion_data = {"cells": [{"cell_id": "STAIR1", "congestion_level": 2.5}]}
    path_hazard, _ = pf.find_path("START", "STAIR2", congestion_data)
    assert path_hazard == []
    
    # Soft congestion multiplier
    congestion_soft = {"cells": [{"cell_id": "STAIR1", "congestion_level": 0.5}]}
    _, cost_soft = pf.find_path("START", "STAIR1", congestion_soft)
    assert cost_soft == pytest.approx(22.5, abs=0.5)
