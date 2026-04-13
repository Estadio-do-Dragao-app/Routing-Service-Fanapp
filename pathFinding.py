import heapq
import math
from typing import Dict, List, Tuple, Optional, Set
import httpx
import logging

logger = logging.getLogger(__name__)


class PathFinder:
    def __init__(self, map_data: dict):
        """
        Initialize with static map data.
        Builds internal structures for fast lookups.
        """
        # Robust Node Loading (Ignoring nodes with 0,0 coordinates)
        self.nodes = {}
        for n in map_data.get('nodes', []):
            nid = n.get('id', n.get('node_id'))
            if nid:
                # NEW: Skip nodes with (0,0) as they break routing logic
                if n.get('x') == 0 and n.get('y') == 0:
                    continue
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
            
            u = edge.get('from', edge.get('source', edge.get('from_id')))
            v = edge.get('to', edge.get('target', edge.get('to_id')))
            w = edge.get('w', edge.get('weight', 1.0))

            if not u or not v:
                continue
            
            if u not in self.graph:
                self.graph[u] = []
            
            # Store base weight, congestion will be applied dynamically
            self.graph[u].append((v, float(w)))

        # FIX: Conectar componentes desconexos (Map Service gera grafos fragmentados)
        self._bridge_disconnected_components()

    def _bridge_disconnected_components(self):
        """
        CRITICAL FIX: Encontra componentes isolados no grafo e cria pontes seguras.
        Regras:
        1. Apenas conecta nós no mesmo piso (level).
        2. Proíbe o uso de POIs como âncoras de pontes (para não quebrar a lógica do A*).
        3. Usa uma abordagem de MST para conectar ilhas com o mínimo de arestas extras.
        """
        from collections import deque
        
        # 1. Encontrar componentes usando BFS (tratando como grafo não-direcionado para conectividade)
        undirected = {}
        for u, neighbors in self.graph.items():
            if u not in undirected: undirected[u] = set()
            for v, _ in neighbors:
                if v not in undirected: undirected[v] = set()
                undirected[u].add(v)
                undirected[v].add(u)

        visited_global = set()
        all_components = []
        
        for node_id in self.nodes.keys():
            if node_id in visited_global:
                continue
            
            component = set()
            queue = deque([node_id])
            visited_global.add(node_id)
            component.add(node_id)
            
            while queue:
                current = queue.popleft()
                for neighbor in undirected.get(current, []):
                    if neighbor not in visited_global:
                        visited_global.add(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
            
            all_components.append(component)

        if len(all_components) <= 1:
            logger.info(f"[GRAPH] Grafo totalmente conectado ({len(self.nodes)} nós)")
            return

        logger.warning(f"[GRAPH BRIDGE] Encontrados {len(all_components)} componentes isolados. Iniciando bridging...")

        # 2. Agrupar componentes por piso
        level_to_components = {}
        for comp in all_components:
            levels = {self.nodes[nid]['level'] for nid in comp}
            for lvl in levels:
                if lvl not in level_to_components:
                    level_to_components[lvl] = []
                level_to_components[lvl].append(comp)

        # 3. Para cada piso, criar pontes MST
        for lvl, comps in level_to_components.items():
            if len(comps) <= 1:
                continue
            
            # Filtro: Nós válidos para pontes (NÃO POIs e no mesmo nível)
            comp_valid_nodes = []
            for comp in comps:
                valid = [nid for nid in comp if not nid.startswith('POI-') and not nid.startswith('OSM-') and self.nodes[nid]['level'] == lvl]
                comp_valid_nodes.append(valid)

            potential_bridges = []
            for i in range(len(comps)):
                for j in range(i + 1, len(comps)):
                    if not comp_valid_nodes[i] or not comp_valid_nodes[j]:
                        continue
                    
                    min_dist = float('inf')
                    best_pair = None
                    
                    for nid_i in comp_valid_nodes[i]:
                        n_i = self.nodes[nid_i]
                        for nid_j in comp_valid_nodes[j]:
                            n_j = self.nodes[nid_j]
                            d = self.calculate_distance(n_i['x'], n_i['y'], n_j['x'], n_j['y'])
                            if d < min_dist:
                                min_dist = d
                                best_pair = (nid_i, nid_j)
                    
                    if best_pair and min_dist < 500: 
                        potential_bridges.append((min_dist, i, j, best_pair))

            # Kruskal para selecionar as melhores pontes
            potential_bridges.sort()
            parent = list(range(len(comps)))
            def find(i):
                if parent[i] == i: return i
                parent[i] = find(parent[i])
                return parent[i]
            
            def union(i, j):
                root_i = find(i)
                root_j = find(j)
                if root_i != root_j:
                    parent[root_i] = root_j
                    return True
                return False

            for dist, i, j, (nid_a, nid_b) in potential_bridges:
                if union(i, j):
                    if nid_a not in self.graph: self.graph[nid_a] = []
                    if nid_b not in self.graph: self.graph[nid_b] = []
                    self.graph[nid_a].append((nid_b, dist))
                    self.graph[nid_b].append((nid_a, dist))
                    logger.warning(f"[GRAPH BRIDGE] Piso {lvl}: Conectado {nid_a} <-> {nid_b} ({dist:.1f}m)")


    @staticmethod
    async def fetch_map_data(client: httpx.AsyncClient, service_url: str):
        response = await client.get(f"{service_url}/map")
        response.raise_for_status()
        return response.json()

    @staticmethod
    async def fetch_congestion_data(client: httpx.AsyncClient, service_url: str):
        response = await client.get(f"{service_url}/heatmap/stadium/cells")
        response.raise_for_status()
        return response.json()

    def calculate_distance(self, x1, y1, x2, y2):
        """Haversine distance for GPS coordinates (returns meters)"""
        # x is Longitude, y is Latitude
        lat1, lon1 = math.radians(y1), math.radians(x1)
        lat2, lon2 = math.radians(y2), math.radians(x2)
        
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        
        a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        r = 6371000  # Radius of earth in meters
        return r * c
    
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
        avoid_stairs: bool = False,
        waittime_data: Optional[Dict[str, float]] = None,
        blocked_nodes: Optional[Set[str]] = None
    ) -> Tuple[List[str], float]:
        """
        A* pathfinding algorithm with cached map, dynamic congestion,
        and wait time penalties for POIs with queues.
        """
        # Pre-process congestion lookup
        congestion_map = self._get_congestion_map(congestion_data)
        
        # Pre-process wait time lookup (default to empty dict if not provided)
        waittime_map = waittime_data or {}
        
        # Closed nodes from static closures + dynamic emergency closures
        closed_nodes = {c['node_id'] for c in self.closures if c.get('node_id')}
        if blocked_nodes:
            closed_nodes.update(blocked_nodes)
        
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
                
                # 1. Block seat nodes as intermediate points (seats can only be start/end)
                neighbor_type = self.nodes.get(neighbor, {}).get('type', '')
                if neighbor_type == 'seat' and neighbor != end_node_id:
                    continue  # Skip seats unless they are the destination
                
                # 2. Check Hazards (Hard Block)
                congestion_level = congestion_map.get(neighbor, 0.0)
                if congestion_level > 2.0: # > 200% congestion = Blocked (Fire/Danger)
                    continue

                # 3. Check Stairs (Accessibility)
                if avoid_stairs:
                    n_from = self.nodes.get(current, {})
                    n_to = self.nodes.get(neighbor, {})
                    
                    # Block usage if both nodes are 'stairs' (the edge between them is the stairs)
                    is_stairs_edge = (n_from.get('type') == 'stairs' and n_to.get('type') == 'stairs')
                    # Also block if moving to a stair on a different level
                    is_vertical_stairs = (n_to.get('type') == 'stairs' and n_from.get('level') != n_to.get('level'))
                    
                    if is_stairs_edge or is_vertical_stairs:
                        continue

                # 3. Calculate Weight (including Soft Congestion Penalty)
                weight = base_weight
                if congestion_level > 0:
                    # Moderate penalty (4x) - Trade-off between delay and detour
                    weight *= (1.0 + (congestion_level * 10.0))
                
                # 4. Apply Wait Time Penalty for POIs with queues
                if neighbor in waittime_map:
                    wait_minutes = waittime_map[neighbor]
                    # Convert wait time to distance equivalent:
                    # At 1.4 m/s walking speed = 84m per minute
                    # So 5 min wait ≈ 420m detour would be acceptable
                    wait_penalty = wait_minutes * 84
                    weight += wait_penalty
                
                tentative_g = current_g + weight
                
                if neighbor not in g_score or tentative_g < g_score[neighbor]:
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative_g
                    f_score = tentative_g + heuristic(neighbor)
                    heapq.heappush(open_set, (f_score, tentative_g, neighbor))
        
        # No path found
        return [], float('inf')