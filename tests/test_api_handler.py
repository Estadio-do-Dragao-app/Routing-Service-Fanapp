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
    if api_handler.state.pathfinder is None:
        mock_pathfinder = MagicMock()
        api_handler.state.pathfinder = mock_pathfinder
    else:
        mock_pathfinder = api_handler.state.pathfinder

    # Pre-populate POI cache for tests
    api_handler.state.poi_cache = [
        {"id": "bar-1", "x": 14.0, "y": 0.0, "level": 0}
    ]

    # Pre-populate waittime cache
    api_handler.state.waittime_cache = {"bar-1": 3.0}

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
        mock_pathfinder, "nodes",
        {
            "A": {"id": "A", "x": 0.0, "y": 0.0, "level": 0},
            "B": {"id": "B", "x": 14.0, "y": 0.0, "level": 0}
        }
    )
    
    # Mock find_nearest_node to return a valid node ID
    monkeypatch.setattr(
        mock_pathfinder, "find_nearest_node",
        MagicMock(return_value="A")
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
    monkeypatch.setattr(api_handler.state, "http_client", mock_client)

    yield


def test_route_poi_success():
    resp = client.post(
        "/route",
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
    monkeypatch.setattr(api_handler.state.pathfinder, "find_path", MagicMock(return_value=([], float("inf"))))
    resp = client.post(
        "/route",
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
        "/route",
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
        "/route",
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


def test_refresh_map_success(monkeypatch):
    """Test manual map refresh endpoint with authorized request."""
    monkeypatch.setattr(api_handler, "_refresh_all_caches", AsyncMock())
    resp = client.post(
        "/refresh_map",
        headers=VALID_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_refresh_map_unauthorized():
    """Test manual map refresh endpoint without authentication."""
    resp = client.post("/refresh_map")
    assert resp.status_code == 401


def test_trigger_alert_success(monkeypatch):
    """Test emergency alert trigger endpoint."""
    monkeypatch.setattr(api_handler, "handle_emergency_alert_async", AsyncMock())
    resp = client.post(
        "/alerts",
        json={
            "alert_type": "FIRE",
            "severity": 5,
            "message": "Fire in sector 4",
            "affected_areas": ["tile-12"],
            "level": 0
        },
        headers=VALID_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"


def test_trigger_alert_unauthorized():
    """Test emergency alert trigger endpoint without authentication."""
    resp = client.post(
        "/alerts",
        json={
            "alert_type": "FIRE",
            "severity": 5,
            "message": "Fire in sector 4",
            "affected_areas": ["tile-12"],
            "level": 0
        }
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_find_alternative_pois_logic():
    """Test the find_alternative_pois logic and _is_poi_match."""
    api_handler.state.poi_cache = [
        {"id": "toilet-1", "x": 10.0, "y": 5.0, "level": 0, "type": "toilet"},
        {"id": "toilet-2", "x": 20.0, "y": 5.0, "level": 0, "type": "toilet"},
        {"id": "food-1", "x": 30.0, "y": 5.0, "level": 0, "type": "food"}
    ]
    api_handler.state.waittime_cache = {"toilet-1": 10, "toilet-2": 2}
    
    alts = await api_handler.find_alternative_pois("toilet-1", "toilet")
    assert len(alts) == 1
    assert alts[0]["id"] == "toilet-2"
    assert alts[0]["wait_time"] == 2


@pytest.mark.asyncio
async def test_handle_waittime_update_flow(monkeypatch):
    """Test handle_waittime_update_async processes payload and triggers check."""
    mock_check = AsyncMock()
    monkeypatch.setattr(api_handler, "check_reroutes_for_waittime_change", mock_check)
    
    mock_session_mgr = MagicMock()
    api_handler.state.session_manager = mock_session_mgr
    
    # Trigger waittime change from 0 to 10
    api_handler.state.waittime_cache["toilet-1"] = 0.0
    await api_handler.handle_waittime_update_async("toilet-1", {"minutes": 10.0})
    
    assert api_handler.state.waittime_cache["toilet-1"] == pytest.approx(10.0)
    mock_check.assert_called_once_with("toilet-1", 10.0)


@pytest.mark.asyncio
async def test_handle_congestion_update_flow(monkeypatch):
    """Test handle_congestion_update_async updates cache and schedules task."""
    mock_check = AsyncMock()
    monkeypatch.setattr(api_handler, "check_reroutes_for_congestion_change", mock_check)
    
    mock_session_mgr = MagicMock()
    api_handler.state.session_manager = mock_session_mgr
    
    # Significant change (from 0 to 0.8)
    api_handler.state.congestion_cache["cell_1"] = 0.0
    await api_handler.handle_congestion_update_async({
        "cell_id": "cell_1",
        "congestion_level": 0.8
    })
    
    assert api_handler.state.congestion_cache["cell_1"] == pytest.approx(0.8)
    # check task scheduled in background_tasks
    assert len(api_handler.state.background_tasks) == 1


@pytest.mark.asyncio
async def test_check_reroutes_for_waittime_change_trigger(monkeypatch):
    """Test wait time change checking triggers reroute recommendations."""
    mock_session = MagicMock()
    mock_session.session_id = "sess-waittime"
    mock_session.destination_type = "poi"
    mock_session.destination_id = "toilet-1"
    mock_session.end_node = "toilet-1"
    mock_session.avoid_stairs = False
    mock_session.estimate_current_position.return_value = ((1.0, 1.0, 0), 0.9)
    
    mock_session_mgr = MagicMock()
    mock_session_mgr.get_active_sessions.return_value = [mock_session]
    api_handler.state.session_manager = mock_session_mgr
    
    # Mock pathfinder nodes and routes
    api_handler.state.pathfinder.find_path.return_value = (["A", "B"], 10.0)
    api_handler.state.pathfinder.nodes = {
        "A": {"id": "A", "x": 0.0, "y": 0.0, "level": 0},
        "B": {"id": "B", "x": 10.0, "y": 0.0, "level": 0}
    }
    
    api_handler.state.poi_cache = [
        {"id": "toilet-1", "x": 0.0, "y": 0.0, "level": 0, "type": "toilet"},
        {"id": "toilet-2", "x": 10.0, "y": 0.0, "level": 0, "type": "toilet"}
    ]
    api_handler.state.waittime_cache = {"toilet-1": 15.0, "toilet-2": 2.0}
    
    # Mock MQTT handler
    mock_mqtt = MagicMock()
    api_handler.state.mqtt_handler = mock_mqtt
    
    await api_handler.check_reroutes_for_waittime_change("toilet-1", 15.0)
    assert mock_mqtt.publish_route_update.called


@pytest.mark.asyncio
async def test_check_reroutes_for_congestion_change_trigger(monkeypatch):
    """Test congestion changes trigger reroute recommendations."""
    mock_session = MagicMock()
    mock_session.session_id = "sess-congestion"
    mock_session.destination_type = "poi"
    mock_session.destination_id = "toilet-1"
    mock_session.end_node = "toilet-1"
    mock_session.avoid_stairs = False
    mock_session.estimate_current_position.return_value = ((1.0, 1.0, 0), 0.9)
    mock_session.current_route = ["A", "B"]
    mock_session.total_cost = 100.0
    
    mock_session_mgr = MagicMock()
    mock_session_mgr.get_active_sessions.return_value = [mock_session]
    mock_session_mgr.should_reroute.return_value = {
        "type": "reroute_suggestion",
        "new_route": ["A", "C", "B"]
    }
    api_handler.state.session_manager = mock_session_mgr
    
    api_handler.state.pathfinder.find_path.return_value = (["A", "C", "B"], 20.0)
    
    mock_mqtt = MagicMock()
    api_handler.state.mqtt_handler = mock_mqtt
    
    await api_handler.check_reroutes_for_congestion_change("cell_1")
    assert mock_mqtt.publish_route_update.called


@pytest.mark.asyncio
async def test_trigger_evacuation_routes_async(monkeypatch):
    """Test evacuation routing for all active sessions."""
    mock_session = MagicMock()
    mock_session.session_id = "sess-evacuation"
    mock_session.destination_type = "poi"
    mock_session.destination_id = "toilet-1"
    mock_session.end_node = "toilet-1"
    mock_session.avoid_stairs = False
    mock_session.estimate_current_position.return_value = ((1.0, 1.0, 0), 0.9)
    
    mock_session_mgr = MagicMock()
    mock_session_mgr.get_active_sessions.return_value = [mock_session]
    api_handler.state.session_manager = mock_session_mgr
    
    api_handler.state.pathfinder.nodes = {
        "A": {"id": "A", "x": 0.0, "y": 0.0, "level": 0},
        "EXIT": {"id": "EXIT", "x": 10.0, "y": 0.0, "level": 0, "type": "emergency_exit"}
    }
    api_handler.state.pathfinder.find_path.return_value = (["A", "EXIT"], 5.0)
    
    mock_mqtt = MagicMock()
    api_handler.state.mqtt_handler = mock_mqtt
    
    await api_handler.trigger_evacuation_routes_async()
    assert mock_mqtt.publish_route_update.called


@pytest.mark.asyncio
async def test_resolve_tiles_to_nodes_success(monkeypatch):
    """Test standard flow for resolving map tile IDs to pathfinder nodes."""
    mock_client = AsyncMock()
    req = httpx.Request("POST", "https://fake")
    mock_client.post.return_value = httpx.Response(200, json={"node_ids": ["N1", "N2"]}, request=req)
    api_handler.state.http_client = mock_client
    
    nodes = await api_handler.resolve_tiles_to_nodes(["tile1"])
    assert nodes == {"N1", "N2"}


@pytest.mark.asyncio
async def test_process_emergency_closures(monkeypatch):
    """Test process emergency closures and update state closures."""
    mock_client = AsyncMock()
    req = httpx.Request("POST", "https://fake")
    mock_client.post.return_value = httpx.Response(200, json={"node_ids": ["N10"]}, request=req)
    api_handler.state.http_client = mock_client
    
    api_handler.state.active_closures = set()
    await api_handler.process_emergency_closures(["tile1"])
    assert "N10" in api_handler.state.active_closures


@pytest.mark.asyncio
async def test_handle_emergency_alert_async(monkeypatch):
    """Test processing a parsed alert updates closures and triggers evacuation."""
    monkeypatch.setattr(api_handler, "process_emergency_closures", AsyncMock())
    monkeypatch.setattr(api_handler, "trigger_evacuation_routes_async", AsyncMock())
    
    payload = {
        "alert_type": "FIRE",
        "affected_areas": ["tile1"],
        "severity": 5
    }
    await api_handler.handle_emergency_alert_async(payload)
    assert api_handler.process_emergency_closures.called
    assert api_handler.trigger_evacuation_routes_async.called


@pytest.mark.asyncio
async def test_sync_state(monkeypatch):
    """Test sync_state pulls standard/OSM POIs and Congestion cells into cache."""
    mock_client = AsyncMock()
    req = httpx.Request("GET", "https://fake")
    resp_pois = httpx.Response(200, json=[{"id": "poi1", "x": 1.0, "y": 2.0, "level": 0}], request=req)
    resp_osm = httpx.Response(200, json={"pois": [{"id": "poi2", "x": 3.0, "y": 4.0, "level": 0}]}, request=req)
    resp_cong = httpx.Response(200, json={"cells": [{"cell_id": "cell1", "congestion_level": 0.5}]}, request=req)
    
    async def mock_get(url, *args, **kwargs):
        if "/pois/osm" in url:
            return resp_osm
        elif "/pois" in url:
            return resp_pois
        elif "/heatmap" in url:
            return resp_cong
        return httpx.Response(404, request=req)
        
    mock_client.get = mock_get
    
    api_handler.state.poi_cache = []
    api_handler.state.congestion_cache = {}
    
    await api_handler.sync_state(mock_client)
    assert len(api_handler.state.poi_cache) == 2
    assert api_handler.state.congestion_cache["cell1"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_refresh_all_caches_success(monkeypatch):
    """Test manual map and cache refresh pulling from map service."""
    mock_client = AsyncMock()
    req = httpx.Request("GET", "https://fake")
    
    # Mock PathFinder.fetch_map_data and sync_state
    monkeypatch.setattr(api_handler.PathFinder, "fetch_map_data", AsyncMock(return_value={"nodes": [], "edges": []}))
    monkeypatch.setattr(api_handler, "sync_state", AsyncMock())
    
    api_handler.state.http_client = mock_client
    await api_handler._refresh_all_caches()
    
    assert isinstance(api_handler.state.pathfinder, api_handler.PathFinder)
    assert api_handler.sync_state.called


def test_calculate_route_nearest_category():
    """Test routing to nearest category of POI and computing best travel + wait times."""
    mock_pf = MagicMock()
    mock_pf.find_nearest_node.return_value = "START_NODE"
    mock_pf.nodes = {
        "START_NODE": {"id": "START_NODE", "x": 0.0, "y": 0.0, "level": 0},
        "POI_NODE": {"id": "POI_NODE", "x": 10.0, "y": 0.0, "level": 0}
    }
    mock_pf.calculate_distance.return_value = 10.0
    mock_pf.find_path.return_value = (["START_NODE", "POI_NODE"], 10.0)
    mock_pf.graph = {"POI_NODE": [("START_NODE", 10.0), ("OTHER", 5.0), ("MORE", 3.0)]}
    
    api_handler.state.pathfinder = mock_pf
    api_handler.state.poi_cache = [
        {"id": "toilet-1", "x": 10.0, "y": 0.0, "level": 0, "type": "toilet"}
    ]
    api_handler.state.waittime_cache = {"toilet-1": 2.0}
    
    mock_session_mgr = MagicMock()
    mock_session_mgr.create_session.return_value.session_id = "sess-cat"
    api_handler.state.session_manager = mock_session_mgr
    
    req_payload = {
        "start": {"x": 0.0, "y": 0.0, "level": 0},
        "destination_type": "nearest_category",
        "destination_id": "toilet",
        "avoid_stairs": False,
        "ticket_id": None
    }
    
    resp = client.post("/route", json=req_payload, headers=VALID_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["wait_time"] == pytest.approx(2.0)
    assert data["session_id"] == "sess-cat"


@pytest.mark.asyncio
async def test_calculate_route_seat_or_gate(monkeypatch):
    """Test routing to a specific seat/gate with client response fallback."""
    mock_client = AsyncMock()
    req = httpx.Request("GET", "https://fake")
    mock_client.get.return_value = httpx.Response(200, json={"x": 5.0, "y": 5.0, "level": 0}, request=req)
    api_handler.state.http_client = mock_client
    
    mock_pf = MagicMock()
    mock_pf.find_nearest_node.return_value = "NODE_1"
    mock_pf.nodes = {
        "NODE_1": {"id": "NODE_1", "x": 0.0, "y": 0.0, "level": 0},
        "NODE_2": {"id": "NODE_2", "x": 5.0, "y": 5.0, "level": 0}
    }
    mock_pf.find_path.return_value = (["NODE_1", "NODE_2"], 7.0)
    mock_pf.calculate_distance.return_value = 7.0
    mock_pf.graph = {}
    api_handler.state.pathfinder = mock_pf
    
    mock_session_mgr = MagicMock()
    mock_session_mgr.create_session.return_value.session_id = "sess-seat"
    api_handler.state.session_manager = mock_session_mgr
    
    req_payload = {
        "start": {"x": 0.0, "y": 0.0, "level": 0},
        "destination_type": "seat",
        "destination_id": "seat-10A",
        "avoid_stairs": False,
        "ticket_id": "t-seat"
    }
    
    resp = client.post("/route", json=req_payload, headers=VALID_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "sess-seat"



