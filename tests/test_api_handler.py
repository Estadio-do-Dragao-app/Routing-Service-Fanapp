import pytest
import httpx
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

import api_handler

client = TestClient(api_handler.app)

@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    # Mock map data
    map_data = {
        "nodes": [
            {"id": "A", "x": 0.0, "y": 0.0, "level": 0, "type": "floor"},
            {"id": "B", "x": 14.0, "y": 0.0, "level": 0, "type": "floor"},
        ],
        "edges": [],
        "closures": [],
    }
    monkeypatch.setattr(api_handler.pathfinder, "get_map_data", AsyncMock(return_value=map_data))
    monkeypatch.setattr(api_handler.pathfinder, "find_nearest_node", AsyncMock(side_effect=["A", "B"]))
    monkeypatch.setattr(api_handler.pathfinder, "find_path", AsyncMock(return_value=(["A", "B"], 14.0)))
    monkeypatch.setattr(api_handler.pathfinder, "calculate_distance", lambda *args, **kwargs: 14.0)
    monkeypatch.setattr(api_handler.pathfinder, "get_congestion_data", AsyncMock(return_value={"cells": []}))
    monkeypatch.setattr(api_handler.pathfinder, "get_congestion_weight", lambda *_: 1.0)

    class FakeClient:
        async def get(self, url: str):
            if "/api/pois/" in url:
                return httpx.Response(200, json={"id": "bar-1", "x": 14.0, "y": 0.0, "level": 0})
            if "/heatmap/cell/" in url:
                return httpx.Response(200, json={"cell_id": "bar-1", "congestion_level": 0.3})
            return httpx.Response(404)

    api_handler.http_client = FakeClient()
    yield

def test_route_poi_success():
    resp = client.post(
        "/api/route",
        json={
            "start": {"x": 1.0, "y": 1.0, "level": 0},
            "destination_type": "poi",
            "destination_id": "bar-1",
            "avoid_stairs": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_distance"] == pytest.approx(14.0)
    assert data["path"][0]["node_id"] == "A"
    assert data["path"][-1]["node_id"] == "B"
    # congestion_level 0.3 → wait_time 3.0 minutes by current formula
    assert data["wait_time"] == pytest.approx(3.0)

def test_route_no_path_returns_404(monkeypatch):
    monkeypatch.setattr(api_handler.pathfinder, "find_path", AsyncMock(return_value=([], float("inf"))))
    resp = client.post(
        "/api/route",
        json={
            "start": {"x": 1.0, "y": 1.0, "level": 0},
            "destination_type": "poi",
            "destination_id": "bar-1",
            "avoid_stairs": False,
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "No path found to destination"

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert "map_service" in body