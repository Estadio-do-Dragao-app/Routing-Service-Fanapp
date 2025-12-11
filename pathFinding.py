import heapq
import math
from typing import Dict, List, Tuple, Optional, Set
import httpx


import heapq
import math
from typing import Dict, List, Tuple, Optional, Set
import httpx


class PathFinder:
    def __init__(self, map_data: dict):
        """
        Initialize with static map data.
        Builds internal structures for fast lookups.
        """
        # Robust Node Loading
        self.nodes = {}
        for n in map_data.get('nodes', []):
            nid = n.get('id', n.get('node_id'))
            if nid:
                self.nodes[nid] = n

        self.edges = map_data.get('edges', [])
        self.closures = map_data.get('closures', [])
        
        # Pre-process edges for adjacency (static part)
        self.graph: Dict[str, List[Tuple[str, float]]] = {}
        
        # Robust Closure Loading
        closure_edge_ids = set()
        for c in self.closures:
            eid = c.get('edge_id', c.get('id'))
            if eid:
                closure_edge_ids.add(eid)
        
        for edge in self.edges:
            eid = edge.get('id', edge.get('edge_id'))
            if eid and eid in closure_edge_ids:
                continue
            
            u = edge.get('from', edge.get('source'))
            v = edge.get('to', edge.get('target'))
            w = edge.get('w', edge.get('weight', 1.0))

            if not u or not v:
                continue
            
            if u not in self.graph:
                self.graph[u] = []
            
            # Store base weight, congestion will be applied dynamically
            self.graph[u].append((v, float(w)))

    @staticmethod
    async def fetch_map_data(client: httpx.AsyncClient, service_url: str):
        response = await client.get(f"{service_url}/api/map")
        response.raise_for_status()
        return response.json()

    @staticmethod
    async def fetch_congestion_data(client: httpx.AsyncClient, service_url: str):
        response = await client.get(f"{service_url}/heatmap/stadium/cells")
        response.raise_for_status()
        return response.json()

    def calculate_distance(self, x1: float, y1: float, x2: float, y2: float) -> float:
        """Euclidean distance between two points"""
        return math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
    
    def find_nearest_node(self, x: float, y: float, level: int) -> Optional[str]:
        """
        Find the nearest node to given coordinates using cached nodes.
        O(N) lookup - fast enough for <10k nodes.
        """
        min_dist = float('inf')
        nearest_node_id = None
        
        for node_id, node in self.nodes.items():
            if node['level'] != level:
                continue
            
            dist = self.calculate_distance(x, y, node['x'], node['y'])
            if dist < min_dist:
                min_dist = dist
                nearest_node_id = node_id
        
        return nearest_node_id
    
    def _get_congestion_map(self, congestion_data: dict) -> Dict[str, float]:
        """Pre-process congestion data into a hash map for O(1) access"""
        if not congestion_data or not congestion_data.get('cells'):
            return {}
        # Support 'cell_id' (new API) or 'id' (legacy/mock)
        return {
            cell.get('cell_id', cell.get('id')): cell['congestion_level'] 
            for cell in congestion_data['cells']
        }

    def find_path(
        self, 
        start_node_id: str, 
        end_node_id: str,
        congestion_data: dict,
        avoid_stairs: bool = False
    ) -> Tuple[List[str], float]:
        """
        A* pathfinding algorithm with cached map and dynamic congestion
        """
        # Pre-process congestion lookup
        congestion_map = self._get_congestion_map(congestion_data)
        
        # Closed nodes from static closures
        closed_nodes = {c['node_id'] for c in self.closures if c.get('node_id')}
        
        # A* algorithm
        def heuristic(node_id: str) -> float:
            n1 = self.nodes[node_id]
            n2 = self.nodes[end_node_id]
            return self.calculate_distance(n1['x'], n1['y'], n2['x'], n2['y'])
        
        # Priority queue: (f_score, g_score, node_id)
        open_set = [(heuristic(start_node_id), 0, start_node_id)]
        came_from: Dict[str, str] = {}
        g_score: Dict[str, float] = {start_node_id: 0}
        
        visited: Set[str] = set()
        
        while open_set:
            _, current_g, current = heapq.heappop(open_set)
            
            if current in visited:
                continue
            
            visited.add(current)
            
            if current == end_node_id:
                # Reconstruct path
                path = []
                while current in came_from:
                    path.append(current)
                    current = came_from[current]
                path.append(start_node_id)
                path.reverse()
                return path, g_score[end_node_id]
            
            if current in closed_nodes:
                continue
            
            if current not in self.graph:
                continue
            
            for neighbor, base_weight in self.graph[current]:
                if neighbor in visited or neighbor in closed_nodes:
                    continue
                
                # 1. Check Hazards (Hard Block)
                congestion_level = congestion_map.get(neighbor, 0.0)
                if congestion_level > 2.0: # > 200% congestion = Blocked (Fire/Danger)
                    continue

                # 2. Check Stairs (Accessibility)
                if avoid_stairs:
                    n_from = self.nodes[current]
                    n_to = self.nodes[neighbor]
                    # Block usage of stairs for VERTICAL travel
                    if n_to.get('type') == 'stairs' and n_from['level'] != n_to['level']:
                        continue

                # 3. Calculate Weight (including Soft Congestion Penalty)
                weight = base_weight
                if congestion_level > 0:
                    # Moderate penalty (4x) - Trade-off between delay and detour
                    weight *= (1.0 + (congestion_level * 4.0))
                
                tentative_g = current_g + weight
                
                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    f_score = tentative_g + heuristic(neighbor)
                    heapq.heappush(open_set, (f_score, tentative_g, neighbor))
        
        # No path found
        return [], float('inf')