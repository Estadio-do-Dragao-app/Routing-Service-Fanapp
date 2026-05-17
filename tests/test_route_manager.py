import pytest
import time
from unittest.mock import MagicMock
from route_manager import RouteSession, RouteSessionManager, Checkpoint
from pathFinding import PathFinder

@pytest.fixture
def dummy_pathfinder():
    pf = MagicMock(spec=PathFinder)
    pf.nodes = {
        "N1": {"id": "N1", "x": 0.0, "y": 0.0, "level": 0},
        "N2": {"id": "N2", "x": 10.0, "y": 0.0, "level": 0},
        "N3": {"id": "N3", "x": 20.0, "y": 0.0, "level": 0}
    }
    pf.calculate_distance.return_value = 10.0
    return pf

def test_route_session_properties():
    session = RouteSession(
        session_id="sess-1",
        ticket_id="t-1",
        start_node="N1",
        end_node="N3",
        destination_type="poi",
        destination_id="toilet-1",
        current_route=["N1", "N2", "N3"],
        total_cost=20.0,
        start_time=time.time(),
        last_heartbeat=time.time(),
        avoid_stairs=False
    )
    
    # Test fallback waypoint estimation
    assert session.current_waypoint == "N2"
    
    # Test expired & stale state
    assert not session.is_expired()
    assert not session.is_stale()
    
    # Test heartbeat updating
    old_hb = session.last_heartbeat
    time.sleep(0.01)
    session.update_heartbeat()
    assert session.last_heartbeat > old_hb
    
    # Test checkpoints
    cp = Checkpoint(node_id="N2", timestamp=time.time())
    session.update_checkpoint(cp)
    assert session.last_checkpoint == cp
    assert session.current_waypoint == "N3"

def test_estimate_current_position(dummy_pathfinder):
    session = RouteSession(
        session_id="sess-1",
        ticket_id="t-1",
        start_node="N1",
        end_node="N3",
        destination_type="poi",
        destination_id="toilet-1",
        current_route=["N1", "N2", "N3"],
        total_cost=20.0,
        start_time=time.time() - 10,  # 10s elapsed
        last_heartbeat=time.time(),
        avoid_stairs=False
    )
    
    pos, confidence = session.estimate_current_position(dummy_pathfinder)
    assert pos == (14.0, 0.0, 0)
    assert confidence == pytest.approx(0.9)

def test_session_manager_crud(dummy_pathfinder):
    manager = RouteSessionManager(pathfinder=dummy_pathfinder)
    
    # Create session
    session = manager.create_session(
        ticket_id="t-123",
        start_node="N1",
        end_node="N3",
        destination_type="poi",
        destination_id="toilet-1",
        route=["N1", "N2", "N3"],
        total_cost=20.0,
        avoid_stairs=False
    )
    
    assert session.ticket_id == "t-123"
    assert manager.get_session(session.session_id) == session
    assert len(manager.get_active_sessions()) == 1
    
    # Heartbeat handling
    manager.handle_heartbeat("t-123", {"timestamp": time.time()})
    assert session.last_heartbeat == pytest.approx(time.time(), abs=1)
    
    # Waypoint handling
    manager.handle_waypoint("t-123", {"node_id": "N2", "timestamp": time.time()})
    assert session.last_checkpoint.node_id == "N2"
    
    # Cancellation handling
    manager.handle_cancellation("t-123", {})
    assert not session.is_active
    assert len(manager.get_active_sessions()) == 0

def test_should_reroute(dummy_pathfinder):
    manager = RouteSessionManager(pathfinder=dummy_pathfinder)
    dummy_pathfinder.find_nearest_node.return_value = "N2"
    session = manager.create_session(
        ticket_id="t-123",
        start_node="N1",
        end_node="N3",
        destination_type="poi",
        destination_id="toilet-1",
        route=["N1", "N2", "N3"],
        total_cost=20.0,  # original cost
        avoid_stairs=False
    )
    
    # If the new route cost is MUCH smaller (e.g. 5 seconds), it should suggest reroute
    suggestion = manager.should_reroute(session, ["N1", "N3"], 5.0, reason="Shorter path found")
    assert suggestion is not None
    assert suggestion["type"] == "reroute_suggestion"
    assert suggestion["new_route"] == ["N1", "N3"]
    assert suggestion["improvement"]["time_saved_seconds"] == 15.0
    
    # If the cost is higher, it should not reroute
    no_suggestion = manager.should_reroute(session, ["N1", "N2", "N3"], 25.0, reason="Longer path")
    assert no_suggestion is None


def test_route_session_edge_cases(dummy_pathfinder):
    # 1. No current_route
    session = RouteSession(
        session_id="sess-edge", ticket_id="t-edge", start_node="N1", end_node="N3",
        destination_type="poi", destination_id="toilet-1", current_route=[],
        total_cost=0, start_time=time.time(), last_heartbeat=time.time()
    )
    assert session.current_waypoint == "N1"
    
    # 2. Checkpoint at last node of route
    session.current_route = ["N1", "N2", "N3"]
    cp = Checkpoint(node_id="N3", timestamp=time.time())
    session.update_checkpoint(cp)
    assert session.current_waypoint == "N3"
    
    # 3. Checkpoint not in current_route (ValueError fallback)
    cp_invalid = Checkpoint(node_id="INVALID", timestamp=time.time() - 40)
    session.update_checkpoint(cp_invalid)
    pos, confidence = session.estimate_current_position(dummy_pathfinder)
    # 40s since checkpoint -> confidence is 0.7
    assert confidence == pytest.approx(0.7)
    
    # 4. Long time since checkpoint -> confidence 0.5 and 0.3
    cp_invalid.timestamp = time.time() - 70
    _, conf_70 = session.estimate_current_position(dummy_pathfinder)
    assert conf_70 == 0.5
    
    cp_invalid.timestamp = time.time() - 100
    _, conf_100 = session.estimate_current_position(dummy_pathfinder)
    assert conf_100 == 0.3


def test_session_manager_edge_cases(dummy_pathfinder):
    manager = RouteSessionManager(pathfinder=dummy_pathfinder)
    
    # 1. Re-creating session with same ticket_id cancels old one
    s1 = manager.create_session("t-same", "N1", "N3", "poi", "toilet-1", ["N1", "N2"], 10.0)
    assert s1.is_active
    s2 = manager.create_session("t-same", "N1", "N3", "poi", "toilet-1", ["N1", "N2"], 10.0)
    assert not s1.is_active
    assert s2.is_active
    
    # 2. Cleanup expired sessions
    s2.SESSION_TIMEOUT = -1  # force expire
    expired = manager.cleanup_expired_sessions()
    assert s2.session_id in expired
    assert s2.session_id not in manager.sessions
    
    # 3. should_reroute stale session
    s3 = manager.create_session("t-stale", "N1", "N3", "poi", "toilet-1", ["N1", "N2"], 10.0)
    s3.HEARTBEAT_TIMEOUT = -1
    assert manager.should_reroute(s3, ["N1", "N3"], 5.0, "reason") is None

