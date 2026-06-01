from pydantic import BaseModel
from typing import Optional, List


class Coordinates(BaseModel):
    x: float
    y: float
    level: int = 0

class RouteRequest(BaseModel):
    start: Coordinates
    destination_type: str  # e.g., "node_id", "coordinates"
    destination_id: str
    avoid_stairs: bool = False
    ticket_id: Optional[str] = None  # For session tracking via ticket

class PathNode(BaseModel):
    node_id: str
    x: float
    y: float
    level: int
    distance_from_start: float
    estimated_time: float
    is_waypoint: bool = False  # Mark important decision points

class RouteResponse(BaseModel):
    path: List[PathNode]
    total_distance: float
    estimated_time: float
    congestion_level: float
    wait_time: Optional[float] = None
    warnings: List[str] = []
    session_id: Optional[str] = None  # For MQTT tracking
    mqtt_topic: Optional[str] = None  # Topic to subscribe for updates
    waypoints: List[str] = []  # Important nodes for position updates
    ticket_id: Optional[str] = None  # Ticket ID for MQTT heartbeats and waypoints

