"""
API HANDLERS - Routing Service
Handles routing requests
"""

from typing import List, Optional, Dict
from fastapi import HTTPException
from pydantic import BaseModel

from astar import (
    Graph, HazardMap, HazardType,
    hazard_aware_astar, find_nearest_node,
    multi_destination_route, find_evacuation_route,
    calculate_eta
)


# ========== REQUEST/RESPONSE MODELS ==========

class RouteRequest(BaseModel):
    from_node: str
    to_node: str
    avoid_crowds: bool = False


class RouteResponse(BaseModel):
    path: List[str]
    distance: float
    eta_seconds: int
    waypoints: List[Dict]


class MultiDestinationRequest(BaseModel):
    start: str
    destinations: List[str]


class NearestRequest(BaseModel):
    target: str
    candidates: List[str]


class HazardUpdate(BaseModel):
    node_id: str
    hazard_type: str  # "smoke", "crowd", "fire", "spill", "structural"
    severity: float = 1.0  # 0.0-1.0


# ========== ROUTE API HANDLER ==========

class RouteAPIHandler:
    """Handles all routing-related API requests"""
    
    def __init__(self, graph: Graph, hazard_map: HazardMap):
        self.graph = graph
        self.hazard_map = hazard_map
    
    def get_route(self, request: RouteRequest) -> RouteResponse:
        """Calculate shortest path between two nodes"""
        
        # Validate nodes exist
        if request.from_node not in self.graph.nodes:
            raise HTTPException(
                status_code=404,
                detail=f"Start node {request.from_node} not found"
            )
        
        if request.to_node not in self.graph.nodes:
            raise HTTPException(
                status_code=404,
                detail=f"End node {request.to_node} not found"
            )
        
        # Calculate path
        path, distance = hazard_aware_astar(
            self.graph,
            request.from_node,
            request.to_node,
            self.hazard_map,
            request.avoid_crowds
        )
        
        if not path:
            raise HTTPException(
                status_code=404,
                detail="No path found (may be blocked by closures)"
            )
        
        # Build waypoints
        waypoints = self._build_waypoints(path)
        
        return RouteResponse(
            path=path,
            distance=round(distance, 2),
            eta_seconds=calculate_eta(distance),
            waypoints=waypoints
        )
    
    def get_multi_destination_route(self, request: MultiDestinationRequest) -> RouteResponse:
        """Calculate route visiting multiple destinations"""
        
        # Validate nodes
        if request.start not in self.graph.nodes:
            raise HTTPException(status_code=404, detail=f"Start node {request.start} not found")
        
        for dest in request.destinations:
            if dest not in self.graph.nodes:
                raise HTTPException(status_code=404, detail=f"Destination {dest} not found")
        
        # Calculate route
        path, distance = multi_destination_route(
            self.graph,
            request.start,
            request.destinations,
            self.hazard_map
        )
        
        if not path:
            raise HTTPException(status_code=404, detail="No path found")
        
        waypoints = self._build_waypoints(path)
        
        return RouteResponse(
            path=path,
            distance=round(distance, 2),
            eta_seconds=calculate_eta(distance),
            waypoints=waypoints
        )
    
    def find_nearest_node_handler(self, request: NearestRequest) -> RouteResponse:
        """Find nearest node from a list of candidates"""
        
        # Validate target
        if request.target not in self.graph.nodes:
            raise HTTPException(status_code=404, detail=f"Target {request.target} not found")
        
        # Validate candidates
        for candidate in request.candidates:
            if candidate not in self.graph.nodes:
                raise HTTPException(status_code=404, detail=f"Candidate {candidate} not found")
        
        # Find nearest
        nearest, path, distance = find_nearest_node(
            self.graph,
            request.target,
            request.candidates,
            self.hazard_map
        )
        
        if not nearest:
            raise HTTPException(status_code=404, detail="No reachable candidate found")
        
        waypoints = self._build_waypoints(path)
        
        return RouteResponse(
            path=path,
            distance=round(distance, 2),
            eta_seconds=calculate_eta(distance),
            waypoints=waypoints
        )
    
    def get_evacuation_route(self, from_node: str, exit_nodes: List[str]) -> RouteResponse:
        """Find safest evacuation route"""
        
        if from_node not in self.graph.nodes:
            raise HTTPException(status_code=404, detail=f"Node {from_node} not found")
        
        path, distance, exit_node = find_evacuation_route(
            self.graph,
            from_node,
            self.hazard_map,
            exit_nodes
        )
        
        if not path:
            raise HTTPException(status_code=404, detail="No evacuation route available")
        
        waypoints = self._build_waypoints(path)
        
        return RouteResponse(
            path=path,
            distance=round(distance, 2),
            eta_seconds=calculate_eta(distance),
            waypoints=waypoints
        )
    
    def _build_waypoints(self, path: List[str]) -> List[Dict]:
        """Build waypoint list with coordinates"""
        waypoints = []
        for node_id in path:
            node = self.graph.nodes[node_id]
            waypoints.append({
                "node_id": node_id,
                "x": node.x,
                "y": node.y,
                "level": node.level,
                "type": node.type
            })
        return waypoints


# ========== HAZARD API HANDLER ==========

class HazardAPIHandler:
    """Handles hazard management API requests"""
    
    def __init__(self, hazard_map: HazardMap):
        self.hazard_map = hazard_map
    
    def add_closure(self, from_node: str, to_node: str):
        """Add corridor closure"""
        self.hazard_map.add_closure(from_node, to_node)
        return {
            "message": "Closure added",
            "from": from_node,
            "to": to_node,
            "bidirectional": True
        }
    
    def remove_closure(self, from_node: str, to_node: str):
        """Remove corridor closure"""
        self.hazard_map.remove_closure(from_node, to_node)
        return {
            "message": "Closure removed",
            "from": from_node,
            "to": to_node
        }
    
    def update_hazard(self, update: HazardUpdate):
        """Update hazard penalty for a node"""
        
        # Parse hazard type
        hazard_type_map = {
            "smoke": HazardType.SMOKE,
            "crowd": HazardType.CROWD,
            "fire": HazardType.FIRE,
            "spill": HazardType.SPILL,
            "structural": HazardType.STRUCTURAL
        }
        
        hazard_type = hazard_type_map.get(update.hazard_type.lower())
        
        if not hazard_type:
            raise HTTPException(status_code=400, detail=f"Invalid hazard type: {update.hazard_type}")
        
        # Clamp severity to [0, 1]
        severity = max(0.0, min(1.0, update.severity))
        
        self.hazard_map.set_node_hazard(update.node_id, hazard_type, severity)
        
        return {
            "message": "Hazard updated",
            "node_id": update.node_id,
            "hazard_type": update.hazard_type,
            "severity": severity,
            "penalty": hazard_type.value * severity
        }
    
    def update_crowd_penalty(self, node_id: str, occupancy_rate: float):
        """Update crowd penalty based on occupancy rate"""
        
        self.hazard_map.set_crowd_penalty(node_id, occupancy_rate)
        penalty = self.hazard_map.get_node_penalty(node_id)
        
        return {
            "message": "Crowd penalty updated",
            "node_id": node_id,
            "occupancy_rate": occupancy_rate,
            "calculated_penalty": round(penalty, 2)
        }
    
    def clear_hazards(self, node_id: str):
        """Clear all hazards from a node"""
        self.hazard_map.clear_node_hazards(node_id)
        return {
            "message": "Hazards cleared",
            "node_id": node_id
        }
    
    def get_hazard_status(self) -> Dict:
        """Get summary of current hazards"""
        return {
            "closures": len(self.hazard_map.closures) // 2,  # Bidirectional
            "node_hazards": len(self.hazard_map.node_hazards),
            "edge_hazards": len(self.hazard_map.edge_hazards) // 2
        }