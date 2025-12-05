"""
ROUTING SERVICE - Main Application
Responsible for calculating routes
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
import httpx
import asyncio

from astar import Graph, HazardMap, hazard_aware_astar, find_nearest_node, multi_destination_route
from api_handlers import RouteAPIHandler, HazardAPIHandler

# ========== FASTAPI APP ==========

app = FastAPI(
    title="Stadium Routing Service",
    description="Hazard-aware pathfinding with A* algorithm",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== CONFIGURATION ==========

MAP_SERVICE_URL = "http://localhost:8000"

# ========== GLOBAL STATE ==========

GRAPH: Optional[Graph] = None
HAZARD_MAP: Optional[HazardMap] = None

# API Handlers (only routing and hazards)
route_handler: Optional[RouteAPIHandler] = None
hazard_handler: Optional[HazardAPIHandler] = None


# ========== STARTUP ==========

@app.on_event("startup")
async def startup():
    """Initialize routing service"""
    global GRAPH, HAZARD_MAP, route_handler, hazard_handler
    
    print("\n" + "="*60)
    print("🚀 ROUTING SERVICE v2.0 - STARTING")
    print("="*60)
    
    # Initialize hazard map
    HAZARD_MAP = HazardMap()
    print("✅ Hazard map initialized")
    
    # Load graph from Map Service
    success = await load_graph_from_map_service()
    
    if success and GRAPH:
        # Initialize API handlers (ONLY route and hazard)
        route_handler = RouteAPIHandler(GRAPH, HAZARD_MAP)
        hazard_handler = HazardAPIHandler(HAZARD_MAP)
        print("✅ API handlers initialized")
        
        print("\n" + "="*60)
        print("✅ ROUTING SERVICE READY")
        print(f"   - Nodes: {len(GRAPH.nodes)}")
        print(f"   - Edges: {sum(len(v) for v in GRAPH.adjacency.values())}")
        print(f"   - Map Service: {MAP_SERVICE_URL}")
        print("="*60 + "\n")
    else:
        print("\n⚠️  WARNING: Could not load graph from Map Service")
        print(f"   Make sure Map Service is running at {MAP_SERVICE_URL}")
        print("   Service will retry on first request\n")


async def load_graph_from_map_service():
    """Fetch graph data from Map Service"""
    global GRAPH, HAZARD_MAP
    
    try:
        print(f"📥 Fetching graph from {MAP_SERVICE_URL}/api/map ...")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{MAP_SERVICE_URL}/api/map")
            data = response.json()
            
            nodes = data.get('nodes', [])
            edges = data.get('edges', [])
            closures = data.get('closures', [])
            
            # Build graph
            GRAPH = Graph(nodes, edges)
            print(f"✅ Graph loaded: {len(nodes)} nodes, {len(edges)} edges")
            
            # Load closures
            if closures:
                for closure in closures:
                    if closure.get('edge_id'):
                        edge = next((e for e in edges if e['id'] == closure['edge_id']), None)
                        if edge:
                            HAZARD_MAP.add_closure(edge['from'], edge['to'])
                print(f"✅ Loaded {len(closures)} closures")
            
            return True
            
    except Exception as e:
        print(f"❌ Error loading graph: {e}")
        return False


# ========== HEALTH & STATUS ==========

@app.get("/")
async def root():
    """Health check"""
    return {
        "service": "Routing Service",
        "version": "2.0.0",
        "status": "running" if GRAPH else "no_graph_loaded",
        "nodes": len(GRAPH.nodes) if GRAPH else 0,
        "closures": len(HAZARD_MAP.closures) // 2 if HAZARD_MAP else 0,
        "map_service": MAP_SERVICE_URL
    }


@app.get("/health")
async def health():
    """Detailed health check"""
    if not GRAPH:
        return {
            "status": "degraded",
            "message": "Graph not loaded",
            "suggestion": "Call POST /api/reload to load graph"
        }
    
    return {
        "status": "healthy",
        "graph": {
            "nodes": len(GRAPH.nodes),
            "adjacency_entries": len(GRAPH.adjacency)
        },
        "hazards": {
            "closures": len(HAZARD_MAP.closures) // 2,
            "node_hazards": len(HAZARD_MAP.node_hazards),
            "edge_hazards": len(HAZARD_MAP.edge_hazards) // 2
        }
    }


@app.post("/api/reload")
async def reload_graph():
    """Reload graph from Map Service"""
    success = await load_graph_from_map_service()
    
    if not success:
        raise HTTPException(status_code=503, detail="Failed to reload graph from Map Service")
    
    # Reinitialize handlers
    global route_handler, hazard_handler
    
    if GRAPH and HAZARD_MAP:
        route_handler = RouteAPIHandler(GRAPH, HAZARD_MAP)
        hazard_handler = HazardAPIHandler(HAZARD_MAP)
    
    return {
        "status": "success",
        "nodes": len(GRAPH.nodes),
        "edges": sum(len(v) for v in GRAPH.adjacency.values())
    }


# ========== ROUTING ENDPOINTS ==========

@app.get("/api/route")
async def get_route(
    from_node: str = Query(..., description="Start node ID"),
    to_node: str = Query(..., description="End node ID"),
    avoid_crowds: bool = Query(False, description="Avoid crowded areas")
):
    """
    Calculate shortest path between two nodes
    
    Example: /api/route?from_node=N1&to_node=N10&avoid_crowds=true
    """
    if not GRAPH:
        await load_graph_from_map_service()
        if not GRAPH:
            raise HTTPException(status_code=503, detail="Graph not loaded")
    
    from api_handlers import RouteRequest
    
    request = RouteRequest(
        from_node=from_node,
        to_node=to_node,
        avoid_crowds=avoid_crowds
    )
    
    return route_handler.get_route(request)


@app.post("/api/route/multi")
async def multi_destination_route(from_node: str = Query(...), to_nodes: List[str] = Query(...)):
    """
    Calculate route visiting multiple destinations
    
    Example: POST /api/route/multi?from_node=N1&to_nodes=N5&to_nodes=N10
    """
    if not GRAPH:
        await load_graph_from_map_service()
    
    from api_handlers import MultiDestinationRequest
    
    request = MultiDestinationRequest(
        start=from_node,
        destinations=to_nodes
    )
    
    return route_handler.get_multi_destination_route(request)


@app.post("/api/route/nearest")
async def find_nearest(target: str = Query(...), candidates: List[str] = Query(...)):
    """
    Find nearest node from a list of candidates
    
    Example: POST /api/route/nearest?target=N42&candidates=N10&candidates=N15
    """
    if not GRAPH:
        await load_graph_from_map_service()
    
    from api_handlers import NearestRequest
    
    request = NearestRequest(
        target=target,
        candidates=candidates
    )
    
    return route_handler.find_nearest_node_handler(request)


@app.get("/api/route/evacuation")
async def evacuation_route(from_node: str = Query(..., description="Current position")):
    """
    Find safest evacuation route to nearest exit
    
    Example: /api/route/evacuation?from_node=N42
    """
    if not GRAPH:
        await load_graph_from_map_service()
    
    # Get gates as exit nodes from Map Service
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{MAP_SERVICE_URL}/api/gates", timeout=5)
            gates = response.json()
            
            # Find nodes near gates (within 20m)
            exit_nodes = []
            for gate in gates:
                min_dist = float('inf')
                closest = None
                for node_id, node in GRAPH.nodes.items():
                    dist = ((node.x - gate['x'])**2 + (node.y - gate['y'])**2)**0.5
                    if dist < min_dist and dist < 20.0:
                        min_dist = dist
                        closest = node_id
                if closest and closest not in exit_nodes:
                    exit_nodes.append(closest)
            
            if not exit_nodes:
                exit_nodes = ['N1', 'N21']
            
        except:
            exit_nodes = ['N1', 'N21', 'N20']
    
    return route_handler.get_evacuation_route(from_node, exit_nodes)


# ========== HAZARD MANAGEMENT ==========

@app.post("/api/hazards/closure")
async def add_closure(from_node: str = Query(...), to_node: str = Query(...)):
    """
    Add corridor closure (e.g., during evacuation)
    
    Example: POST /api/hazards/closure?from_node=N2&to_node=N3
    """
    return hazard_handler.add_closure(from_node, to_node)


@app.delete("/api/hazards/closure")
async def remove_closure(from_node: str = Query(...), to_node: str = Query(...)):
    """Remove corridor closure"""
    return hazard_handler.remove_closure(from_node, to_node)


@app.post("/api/hazards/update")
async def update_hazard(node_id: str = Query(...), hazard_type: str = Query(...), severity: float = Query(1.0)):
    """
    Update hazard penalty for a node
    
    Example: POST /api/hazards/update?node_id=N42&hazard_type=smoke&severity=0.8
    
    Hazard types: smoke, crowd, fire, spill, structural
    """
    from api_handlers import HazardUpdate
    
    update = HazardUpdate(
        node_id=node_id,
        hazard_type=hazard_type,
        severity=severity
    )
    
    return hazard_handler.update_hazard(update)


@app.post("/api/hazards/crowd")
async def update_crowd(
    node_id: str = Query(...),
    occupancy_rate: float = Query(..., ge=0, le=100)
):
    """
    Update crowd penalty based on occupancy rate
    
    Example: POST /api/hazards/crowd?node_id=N42&occupancy_rate=85
    """
    return hazard_handler.update_crowd_penalty(node_id, occupancy_rate)


@app.delete("/api/hazards/clear")
async def clear_hazards(node_id: str = Query(...)):
    """Clear all hazards from a node"""
    return hazard_handler.clear_hazards(node_id)


@app.get("/api/hazards/status")
async def hazard_status():
    """Get summary of current hazards"""
    return hazard_handler.get_hazard_status()


# ========== RUN SERVER ==========

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8002,
        log_level="info"
    )