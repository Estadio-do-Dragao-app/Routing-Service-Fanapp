"""
A* PATHFINDING WITH HAZARD AWARENESS
Implements hazard-aware routing with dynamic penalties
"""

import math
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass
from enum import Enum


class HazardType(Enum):
    """Hazard types with associated penalties"""
    SMOKE = 5.0
    CROWD = 3.0
    FIRE = 10.0
    SPILL = 2.0
    STRUCTURAL = 8.0


@dataclass
class Node:
    """Graph node representation"""
    id: str
    x: float
    y: float
    level: int = 0
    type: str = "normal"


@dataclass
class Edge:
    """Graph edge representation"""
    from_id: str
    to_id: str
    weight: float  # Base distance/cost


class HazardMap:
    """
    Manages hazards and closures in the stadium
    
    Hazard penalties are ADDED to edge weights:
    - smoke: +5 meters equivalent
    - crowd: +3 meters equivalent  
    - fire: +10 meters equivalent
    """
    
    def __init__(self):
        self.closures: Set[Tuple[str, str]] = set()
        self.node_hazards: Dict[str, Dict[HazardType, float]] = {}
        self.edge_hazards: Dict[Tuple[str, str], Dict[HazardType, float]] = {}
    
    def add_closure(self, from_node: str, to_node: str):
        """Close a corridor (bidirectional)"""
        self.closures.add((from_node, to_node))
        self.closures.add((to_node, from_node))
    
    def remove_closure(self, from_node: str, to_node: str):
        """Reopen a corridor"""
        self.closures.discard((from_node, to_node))
        self.closures.discard((to_node, from_node))
    
    def is_closed(self, from_node: str, to_node: str) -> bool:
        """Check if edge is closed"""
        return (from_node, to_node) in self.closures
    
    def set_node_hazard(self, node_id: str, hazard_type: HazardType, severity: float = 1.0):
        """
        Add hazard to a node
        severity: 0.0-1.0 multiplier (1.0 = full penalty)
        """
        if node_id not in self.node_hazards:
            self.node_hazards[node_id] = {}
        self.node_hazards[node_id][hazard_type] = severity
    
    def set_edge_hazard(self, from_node: str, to_node: str, hazard_type: HazardType, severity: float = 1.0):
        """Add hazard to an edge (bidirectional)"""
        for edge in [(from_node, to_node), (to_node, from_node)]:
            if edge not in self.edge_hazards:
                self.edge_hazards[edge] = {}
            self.edge_hazards[edge][hazard_type] = severity
    
    def set_crowd_penalty(self, node_id: str, occupancy_rate: float):
        """
        Set crowd penalty based on occupancy rate (0-100%)
        Maps to severity: 0-50% -> 0, 50-80% -> 0.5, 80-100% -> 1.0
        """
        if occupancy_rate < 50:
            severity = 0.0
        elif occupancy_rate < 80:
            severity = (occupancy_rate - 50) / 30 * 0.5
        else:
            severity = 0.5 + (occupancy_rate - 80) / 20 * 0.5
        
        self.set_node_hazard(node_id, HazardType.CROWD, min(1.0, severity))
    
    def get_node_penalty(self, node_id: str) -> float:
        """Calculate total penalty for a node"""
        if node_id not in self.node_hazards:
            return 0.0
        
        total = 0.0
        for hazard_type, severity in self.node_hazards[node_id].items():
            total += hazard_type.value * severity
        
        return total
    
    def get_edge_penalty(self, from_node: str, to_node: str) -> float:
        """Calculate total penalty for an edge"""
        edge = (from_node, to_node)
        
        if edge not in self.edge_hazards:
            return 0.0
        
        total = 0.0
        for hazard_type, severity in self.edge_hazards[edge].items():
            total += hazard_type.value * severity
        
        return total
    
    def clear_node_hazards(self, node_id: str):
        """Remove all hazards from a node"""
        self.node_hazards.pop(node_id, None)
    
    def clear_all_hazards(self):
        """Reset all hazards (keep closures)"""
        self.node_hazards.clear()
        self.edge_hazards.clear()


class Graph:
    """
    Stadium graph representation
    Supports multi-level navigation (stairs, elevators)
    """
    
    def __init__(self, nodes: List[Dict], edges: List[Dict]):
        self.nodes: Dict[str, Node] = {}
        self.adjacency: Dict[str, List[Tuple[str, float]]] = {}
        
        # Build nodes
        for n in nodes:
            node = Node(
                id=n['id'],
                x=n['x'],
                y=n['y'],
                level=n.get('level', 0),
                type=n.get('type', 'normal')
            )
            self.nodes[node.id] = node
        
        # Build adjacency list
        for e in edges:
            from_id = e['from']
            to_id = e['to']
            weight = e['w']
            
            if from_id not in self.adjacency:
                self.adjacency[from_id] = []
            
            self.adjacency[from_id].append((to_id, weight))
    
    def get_neighbors(self, node_id: str) -> List[Tuple[str, float]]:
        """Get neighbors of a node with their edge weights"""
        return self.adjacency.get(node_id, [])
    
    def get_node(self, node_id: str) -> Optional[Node]:
        """Get node by ID"""
        return self.nodes.get(node_id)
    
    def heuristic(self, node1_id: str, node2_id: str) -> float:
        """
        Euclidean distance heuristic (admissible for A*)
        Accounts for level changes (stairs add vertical distance)
        """
        n1 = self.nodes.get(node1_id)
        n2 = self.nodes.get(node2_id)
        
        if not n1 or not n2:
            return 0.0
        
        # Horizontal distance
        dx = n1.x - n2.x
        dy = n1.y - n2.y
        horizontal = math.sqrt(dx*dx + dy*dy)
        
        # Vertical distance (stairs cost ~1.5x horizontal distance)
        level_diff = abs(n1.level - n2.level)
        vertical = level_diff * 5.0  # Each level = ~5m equivalent
        
        return horizontal + vertical


def hazard_aware_astar(
    graph: Graph,
    start: str,
    goal: str,
    hazard_map: HazardMap,
    avoid_crowds: bool = False
) -> Tuple[Optional[List[str]], float]:
    """
    HAZARD-AWARE A* PATHFINDING
    
    Algorithm:
    1. Initialize open set with start node
    2. For each node, calculate:
       - g_score = actual cost from start (distance + penalties)
       - h_score = heuristic estimate to goal
       - f_score = g_score + h_score
    3. Always expand node with lowest f_score
    4. Skip closed edges
    5. Add hazard penalties to edge costs
    6. Return path and total cost
    
    Penalties:
    - smoke: +5
    - crowd: +3 (if avoid_crowds=True)
    - fire: +10
    - spill: +2
    - structural: +8
    
    Args:
        graph: Stadium graph
        start: Start node ID
        goal: Goal node ID
        hazard_map: Current hazards and closures
        avoid_crowds: Whether to apply crowd penalties
    
    Returns:
        (path, total_cost) or (None, inf) if no path exists
    """
    
    if start not in graph.nodes or goal not in graph.nodes:
        return None, float('inf')
    
    # Open set: nodes to explore
    open_set: Set[str] = {start}
    
    # Came from: reconstruct path
    came_from: Dict[str, str] = {}
    
    # g_score: cost from start to node
    g_score: Dict[str, float] = {start: 0.0}
    
    # f_score: estimated total cost (g + h)
    f_score: Dict[str, float] = {start: graph.heuristic(start, goal)}
    
    while open_set:
        # Get node with lowest f_score
        current = min(open_set, key=lambda n: f_score.get(n, float('inf')))
        
        # Goal reached
        if current == goal:
            path = _reconstruct_path(came_from, current)
            return path, g_score[goal]
        
        open_set.remove(current)
        
        # Explore neighbors
        for neighbor, base_weight in graph.get_neighbors(current):
            # SKIP CLOSED EDGES
            if hazard_map.is_closed(current, neighbor):
                continue
            
            # Calculate edge cost with penalties
            edge_cost = base_weight
            
            # Add edge hazards
            edge_cost += hazard_map.get_edge_penalty(current, neighbor)
            
            # Add node hazards (destination node)
            if avoid_crowds or HazardType.FIRE in hazard_map.node_hazards.get(neighbor, {}):
                edge_cost += hazard_map.get_node_penalty(neighbor)
            
            # Calculate tentative g_score
            tentative_g = g_score[current] + edge_cost
            
            # Update if better path found
            if neighbor not in g_score or tentative_g < g_score[neighbor]:
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                f_score[neighbor] = tentative_g + graph.heuristic(neighbor, goal)
                
                if neighbor not in open_set:
                    open_set.add(neighbor)
    
    # No path found
    return None, float('inf')


def _reconstruct_path(came_from: Dict[str, str], current: str) -> List[str]:
    """Reconstruct path from came_from map"""
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path


def find_nearest_node(
    graph: Graph,
    target: str,
    candidates: List[str],
    hazard_map: HazardMap
) -> Tuple[Optional[str], Optional[List[str]], float]:
    """
    Find nearest candidate to target
    
    Args:
        graph: Stadium graph
        target: Target node ID
        candidates: List of candidate node IDs
        hazard_map: Current hazards
    
    Returns:
        (nearest_id, path, distance)
    """
    best_candidate = None
    best_path = None
    best_distance = float('inf')
    
    for candidate in candidates:
        path, distance = hazard_aware_astar(graph, target, candidate, hazard_map)
        
        if path and distance < best_distance:
            best_candidate = candidate
            best_path = path
            best_distance = distance
    
    return best_candidate, best_path, best_distance


def multi_destination_route(
    graph: Graph,
    start: str,
    destinations: List[str],
    hazard_map: HazardMap
) -> Tuple[Optional[List[str]], float]:
    """
    Calculate route visiting multiple destinations (greedy nearest neighbor)
    
    Use case: Cleaning staff visiting multiple full bins
    
    Algorithm:
    1. Start at start node
    2. Find nearest unvisited destination
    3. Add path to route
    4. Repeat until all destinations visited
    
    Note: This is a greedy approximation, not optimal TSP solution
    
    Returns:
        (full_path, total_distance)
    """
    if not destinations:
        return [start], 0.0
    
    current = start
    remaining = set(destinations)
    full_path = [start]  # Start with starting node
    total_distance = 0.0
    
    while remaining:
        # Find nearest unvisited destination from CURRENT position
        best_dest = None
        best_path = None
        best_dist = float('inf')
        
        for dest in remaining:
            path, dist = hazard_aware_astar(graph, current, dest, hazard_map)
            if path and dist < best_dist:
                best_dest = dest
                best_path = path
                best_dist = dist
        
        if not best_dest:
            # No path to remaining destinations
            return None, float('inf')
        
        # Add path (skip first node to avoid duplicate)
        full_path.extend(best_path[1:])
        
        total_distance += best_dist
        current = best_dest
        remaining.remove(best_dest)
    
    return full_path, total_distance


def find_evacuation_route(
    graph: Graph,
    start: str,
    hazard_map: HazardMap,
    exit_nodes: List[str]
) -> Tuple[Optional[List[str]], float, Optional[str]]:
    """
    Find safest evacuation route to nearest exit
    
    Args:
        graph: Stadium graph
        start: Current position
        hazard_map: Current hazards (fire, smoke, etc.)
        exit_nodes: List of exit/gate nodes
    
    Returns:
        (path, distance, exit_node)
    """
    nearest_exit, path, distance = find_nearest_node(
        graph, start, exit_nodes, hazard_map
    )
    
    return path, distance, nearest_exit


def calculate_eta(distance: float, speed: float = 1.5) -> int:
    """
    Calculate ETA in seconds
    
    Args:
        distance: Distance in meters
        speed: Walking speed in m/s (default: 1.5 m/s ~= 5.4 km/h)
    
    Returns:
        ETA in seconds
    """
    return int(distance / speed)


def calculate_eta_with_role(distance: float, role: str) -> int:
    """
    Calculate ETA based on staff role
    
    Different roles move at different speeds:
    - security: 2.0 m/s (running)
    - cleaning: 1.2 m/s (carrying equipment)
    - supervisor: 1.5 m/s (walking)
    
    Args:
        distance: Distance in meters
        role: Staff role
    
    Returns:
        ETA in seconds
    """
    speeds = {
        'security': 2.0,    # Running/urgent
        'cleaning': 1.2,    # Slower with equipment
        'supervisor': 1.5,  # Normal walking
        'medical': 1.8      # Fast walking
    }
    
    speed = speeds.get(role.lower(), 1.5)
    return calculate_eta(distance, speed)