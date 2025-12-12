from fastapi import FastAPI, HTTPException
from typing import Optional
from contextlib import asynccontextmanager
import httpx
import os
from dotenv import load_dotenv

from models import RouteRequest, RouteResponse, PathNode, Coordinates
from pathFinding import PathFinder

load_dotenv()

MAP_SERVICE_URL = os.getenv("MAP_SERVICE_URL", "http://localhost:8000")
CONGESTION_SERVICE_URL = os.getenv("CONGESTION_SERVICE_URL", "http://localhost:8001")

http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup resources on startup/shutdown"""
    global http_client
    print("Starting Routing Service...")
    http_client = httpx.AsyncClient(timeout=10.0)
    yield
    print("Shutting down Routing Service...")
    await http_client.aclose()

app = FastAPI(
    title="Routing Service Fanapp",
    version="1.0.0",
    lifespan=lifespan
)

pathfinder: Optional[PathFinder] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup resources on startup/shutdown"""
    global http_client, pathfinder
    print("Starting Routing Service...")
    http_client = httpx.AsyncClient(timeout=10.0)
    
    # Pre-fetch map data for hybrid architecture
    try:
        print("📥 Fetching map data for cache...")
        map_data = await PathFinder.fetch_map_data(http_client, MAP_SERVICE_URL)
        pathfinder = PathFinder(map_data)
        print(f"✅ Map cached: {len(pathfinder.nodes)} nodes")
    except Exception as e:
        print(f"❌ Failed to load map data: {e}")
        # We might want to exit here or retry, but for now we'll let it fail on requests
    
    yield
    print("Shutting down Routing Service...")
    await http_client.aclose()

app = FastAPI(
    title="Routing Service Fanapp",
    version="1.0.0",
    lifespan=lifespan
)

@app.post("/api/route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    """
    Calculate optimal route from user coordinates to destination
    """
    try:
        global http_client, pathfinder
        if http_client is None:
            http_client = httpx.AsyncClient(timeout=10.0)
            
        if pathfinder is None:
             raise HTTPException(status_code=503, detail="Routing service not initialized (Map data missing)")

        # 1. Fetch dynamic congestion data (Concurrent with other logic if we had more)
        try:
            congestion_data = await PathFinder.fetch_congestion_data(http_client, CONGESTION_SERVICE_URL)
        except Exception:
             # Fallback to empty congestion if service down (graceful degradation)
             congestion_data = {"cells": []}
             print("⚠️ Warning: Congestion service unavailable, using valid map data only.")

        # 2. Find nearest node to user's starting position (LOCAL LOOKUP)
        start_node_id = pathfinder.find_nearest_node(
            request.start.x, 
            request.start.y, 
            request.start.level
        )
        
        if not start_node_id:
            raise HTTPException(status_code=404, detail="No nearby node found for starting position")
        
        # 3. Get destination node based on type
        end_node_id = None
        wait_time = None
        
        if request.destination_type == "node":
            end_node_id = request.destination_id
        
        elif request.destination_type == "poi":
            # POI lookup still needs Map Service unless we cache POIs too. 
            # For now, we assume POIs might change or are external entity.
            poi_response = await http_client.get(
                f"{MAP_SERVICE_URL}/pois/{request.destination_id}"
            )
            if poi_response.status_code != 200:
                raise HTTPException(status_code=404, detail="POI not found")
            poi = poi_response.json()
            
            end_node_id = pathfinder.find_nearest_node(
                poi['x'], poi['y'], poi['level']
            )
            
            # Get queue wait time for this POI from Congestion Service
            # We can check our bulk congestion_data first
            # But specific endpoint might be more accurate for 'wait time' specifically
            try:
                congestion_response = await http_client.get(
                    f"{CONGESTION_SERVICE_URL}/heatmap/cell/{request.destination_id}"
                )
                if congestion_response.status_code == 200:
                    cell_data = congestion_response.json()
                    wait_time = cell_data['congestion_level'] * 10
            except Exception:
                wait_time = None
        
        elif request.destination_type in ["seat", "gate"]:
            endpoint = f"/{request.destination_type}s/{request.destination_id}"
            response = await http_client.get(f"{MAP_SERVICE_URL}{endpoint}")
            if response.status_code != 200:
                raise HTTPException(status_code=404, detail=f"{request.destination_type.title()} not found")
            dest = response.json()
            
            end_node_id = pathfinder.find_nearest_node(
                dest['x'], dest['y'], dest['level']
            )
        
        if not end_node_id:
            raise HTTPException(status_code=404, detail="Destination node not found")
        
        # 4. Calculate path (Internal logic + Dynamic Congestion)
        path_ids, total_cost = pathfinder.find_path(
            start_node_id,
            end_node_id,
            congestion_data,
            avoid_stairs=request.avoid_stairs
        )
        
        if not path_ids:
            raise HTTPException(status_code=404, detail="No path found to destination")
        
        # 5. Build response
        path_nodes = []
        cumulative_distance = 0
        
        for i, node_id in enumerate(path_ids):
            node = pathfinder.nodes[node_id]
            
            if i > 0:
                prev_node = pathfinder.nodes[path_ids[i-1]]
                cumulative_distance += pathfinder.calculate_distance(
                    prev_node['x'], prev_node['y'],
                    node['x'], node['y']
                )
            
            path_nodes.append(PathNode(
                node_id=node_id,
                x=node['x'],
                y=node['y'],
                level=node['level'],
                distance_from_start=cumulative_distance,
                estimated_time=cumulative_distance / 1.4
            ))
        
        # Calculate average congestion
        congestion_map = {c.get('cell_id', c.get('id')): c['congestion_level'] for c in congestion_data.get('cells', [])}
        avg_congestion = sum(congestion_map.get(nid, 0.0) for nid in path_ids) / len(path_ids) if path_ids else 1.0
        # Normalize: Base is 0.0 in map but logic uses 0.0-1.0
        # If map returns 0.0-1.0 directly:
        
        return RouteResponse(
            path=path_nodes,
            total_distance=cumulative_distance,
            estimated_time=cumulative_distance / 1.4 + (wait_time or 0) * 60,
            congestion_level=avg_congestion,
            wait_time=wait_time,
            warnings=["High congestion"] if avg_congestion > 0.7 else []
        )
    
    except HTTPException:
        raise
    except Exception as e:
        print(f" Route calculation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error calculating route: {str(e)}")

@app.get("/health")
async def health_check():
    """Service health check endpoint"""
    return {
        "status": "healthy",
        "service": "routing",
        "map_service": MAP_SERVICE_URL,
        "congestion_service": CONGESTION_SERVICE_URL
    }