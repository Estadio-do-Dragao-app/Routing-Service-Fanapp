import pytest
import httpx
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import api_handler

client = TestClient(api_handler.app)
TEST_API_KEY = api_handler.API_KEY  # Use the key from api_handler
VALID_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    # Mock pathfinder if not already initialized
    if api_handler.pathfinder is None:
        mock_pathfinder = MagicMock()
        api_handler.pathfinder = mock_pathfinder
    else:
        mock_pathfinder = api_handler.pathfinder

    # Pre-populate POI cache for tests
    api_handler.poi_cache = [
        {"id": "bar-1", "x": 14.0, "y": 0.0, "level": 0}
    ]

    # Pre-populate waittime cache
    api_handler.waittime_cache = {"bar-1": 3.0}

    # Mock map data
    monkeypatch.setattr(
        mock_pathfinder, "find_path",
        MagicMock(return_value=(["A", "B"], 14.0))
    )
    monkeypatch.setattr(
        mock_pathfinder, "calculate_distance",
        lambda *args, **kwargs: 14.0
    )
    monkeypatch.setattr(
        mock_pathfinder, "get_congestion_data",
        AsyncMock(return_value={"cells": []})
    )
    monkeypatch.setattr(
        mock_pathfinder, "get_congestion_weight",
        lambda *_: 1.0
    )

    # Fake HTTP client using AsyncMock
    async def fake_get(url: str, **kwargs):
        if "/api/pois/" in url:
            return httpx.Response(200, json={"id": "bar-1", "x": 14.0, "y": 0.0, "level": 0})
        if "/heatmap/cell/" in url:
            return httpx.Response(200, json={"cell_id": "bar-1", "congestion_level": 0.3})
        return httpx.Response(404)

    mock_client = AsyncMock()
    mock_client.get = fake_get
    monkeypatch.setattr(api_handler, "http_client", mock_client)

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
        headers=VALID_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_distance"] == pytest.approx(14.0)
    assert data["path"][0]["node_id"] == "A"
    assert data["path"][-1]["node_id"] == "B"
    # congestion_level 0.3 → wait_time 3.0 minutes by current formula
    assert data["wait_time"] == pytest.approx(3.0)


def test_route_no_path_returns_404(monkeypatch):
    monkeypatch.setattr(api_handler.pathfinder, "find_path", MagicMock(return_value=([], float("inf"))))
    resp = client.post(
        "/api/route",
        json={
            "start": {"x": 1.0, "y": 1.0, "level": 0},
            "destination_type": "poi",
            "destination_id": "bar-1",
            "avoid_stairs": False,
        },
        headers=VALID_HEADERS,
    )
    assert resp.status_code == 404
    assert "path found" in resp.json()["detail"].lower()


def test_route_unauthorized():
    """Test that requests without API key are rejected."""
    resp = client.post(
        "/api/route",
        json={
            "start": {"x": 1.0, "y": 1.0, "level": 0},
            "destination_type": "poi",
            "destination_id": "bar-1",
            "avoid_stairs": False,
        },
    )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


def test_route_invalid_api_key():
    """Test that requests with invalid API key are rejected."""
    resp = client.post(
        "/api/route",
        json={
            "start": {"x": 1.0, "y": 1.0, "level": 0},
            "destination_type": "poi",
            "destination_id": "bar-1",
            "avoid_stairs": False,
        },
        headers={"X-API-Key": "invalid_key"},
    )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert "map_service" in body