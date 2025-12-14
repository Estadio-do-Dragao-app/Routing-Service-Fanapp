from fastapi import FastAPI, HTTPException
from typing import Optional, Dict
from contextlib import asynccontextmanager
import httpx
import os
import asyncio
import logging
from dotenv import load_dotenv

from models import RouteRequest, RouteResponse, PathNode, Coordinates
from pathFinding import PathFinder
from route_manager import RouteSessionManager
from mqtt_handler import MQTTRoutingHandler

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAP_SERVICE_URL = os.getenv("MAP_SERVICE_URL", "http://localhost:8000")
CONGESTION_SERVICE_URL = os.getenv("CONGESTION_SERVICE_URL", "http://localhost:8001")
CLIENT_BROKER = os.getenv("CLIENT_BROKER", "localhost")
CLIENT_PORT = int(os.getenv("CLIENT_PORT", 1884))

http_client: Optional[httpx.AsyncClient] = None
pathfinder: Optional[PathFinder] = None
session_manager: Optional[RouteSessionManager] = None
mqtt_handler: Optional[MQTTRoutingHandler] = None
cleanup_task = None

# Cache for wait times received via MQTT (poi_id -> wait_minutes)
waittime_cache: Dict[str, float] = {}

# Cache for congestion data received via MQTT (cell_id -> congestion_level)
congestion_cache: Dict[str, float] = {}


def handle_waittime_update(poi_id: str, payload: dict):
    """Handle wait time updates from MQTT broker and trigger rerouting if needed"""
    global waittime_cache
    try:
        # Support multiple payload formats:
        # - Wait Time Service uses "minutes"
        # - Fallback to "wait_minutes" or "wait_time"
        wait_minutes = payload.get('minutes', payload.get('wait_minutes', payload.get('wait_time', 0)))
        old_wait = waittime_cache.get(poi_id, 0)
        waittime_cache[poi_id] = float(wait_minutes)
        logger.info(f"[WAITTIME] Updated cache: {poi_id} = {wait_minutes} min")
        
        # Trigger rerouting check if wait time changed significantly (> 2 min difference)
        if abs(wait_minutes - old_wait) > 2 and session_manager and pathfinder:
            check_reroutes_for_waittime_change(poi_id, wait_minutes)
            
    except Exception as e:
        logger.error(f"[WAITTIME] Failed to process update for {poi_id}: {e}")


def check_reroutes_for_waittime_change(poi_id: str, new_wait_minutes: float):
    """Check if active sessions need rerouting due to wait time change"""
    if not session_manager or not pathfinder or not mqtt_handler:
        return
    
    active_sessions = session_manager.get_active_sessions()
    
    for session in active_sessions:
        # Check if POI is in current route
        if poi_id not in session.current_route:
            continue
        
        # Estimate current position
        estimated_node, confidence = session.estimate_current_position(pathfinder)
        
        # Only recalculate if we have good position confidence
        if confidence < 0.5:
            continue
        
        try:
            # Recalculate route from estimated position
            new_route, new_cost = pathfinder.find_path(
                estimated_node,
                session.end_node,
                {"cells": []},  # Use empty congestion (will be refreshed separately)
                avoid_stairs=session.avoid_stairs,
                waittime_data=waittime_cache
            )
            
            if not new_route:
                continue
            
            # Check if rerouting is beneficial
            suggestion = session_manager.should_reroute(
                session, new_route, new_cost,
                reason=f"Wait time at {poi_id} changed to {new_wait_minutes:.0f} min"
            )
            
            if suggestion:
                mqtt_handler.publish_route_update(session.session_id, suggestion)
                logger.info(f"[REROUTE] Suggested new route for session {session.session_id}")
                
        except Exception as e:
            logger.error(f"[REROUTE] Failed to check reroute for {session.session_id}: {e}")


def handle_congestion_update(payload: dict):
    """Handle congestion updates from MQTT broker and trigger rerouting if needed"""
    global congestion_cache
    try:
        cell_id = payload.get('cell_id')
        new_level = payload.get('congestion_level', 0)
        
        if not cell_id:
            return
        
        old_level = congestion_cache.get(cell_id, 0)
        congestion_cache[cell_id] = float(new_level)
        
        # Trigger rerouting check if congestion changed significantly (> 20% difference)
        level_diff = abs(new_level - old_level)
        if level_diff > 0.2 and session_manager and pathfinder:
            logger.info(f"[CONGESTION] Cell {cell_id} changed: {old_level:.2f} -> {new_level:.2f}")
            check_reroutes_for_congestion_change(cell_id, new_level)
            
    except Exception as e:
        logger.error(f"[CONGESTION] Failed to process update: {e}")


def check_reroutes_for_congestion_change(cell_id: str, new_congestion: float):
    """Check if active sessions need rerouting due to congestion change"""
    if not session_manager or not pathfinder or not mqtt_handler:
        return
    
    active_sessions = session_manager.get_active_sessions()
    
    for session in active_sessions:
        # Estimate current position
        estimated_node, confidence = session.estimate_current_position(pathfinder)
        
        # Only recalculate if we have reasonable position confidence
        if confidence < 0.4:
            continue
        
        try:
            # Build congestion data for pathfinding from cache
            congestion_data = {"cells": [
                {"cell_id": cid, "congestion_level": level}
                for cid, level in congestion_cache.items()
            ]}
            
            # Recalculate route from estimated position with current congestion
            new_route, new_cost = pathfinder.find_path(
                estimated_node,
                session.end_node,
                congestion_data,
                avoid_stairs=session.avoid_stairs,
                waittime_data=waittime_cache
            )
            
            if not new_route:
                continue
            
            # Check if rerouting is beneficial
            suggestion = session_manager.should_reroute(
                session, new_route, new_cost,
                reason=f"Congestion changed in area (cell {cell_id})"
            )
            
            if suggestion:
                mqtt_handler.publish_route_update(session.session_id, suggestion)
                logger.info(f"[REROUTE] Suggested new route for session {session.session_id} due to congestion")
                
        except Exception as e:
            logger.error(f"[REROUTE] Failed to check congestion reroute for {session.session_id}: {e}")


async def cleanup_sessions():
    """Periodically cleanup expired sessions"""
    while True:
        await asyncio.sleep(60)  # Every minute
        if session_manager:
            expired = session_manager.cleanup_expired_sessions()
            if expired and mqtt_handler:
                for session_id in expired:
                    # Notify client that session expired
                    mqtt_handler.publish_route_update(
                        session_id,
                        {"type": "session_expired", "reason": "timeout"}
                    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup resources on startup/shutdown"""
    global http_client, pathfinder, session_manager, mqtt_handler, cleanup_task
    
    logger.info("=" * 50)
    logger.info("Starting Routing Service...")
    logger.info("=" * 50)
    
    http_client = httpx.AsyncClient(timeout=10.0)
    
    # Pre-fetch map data for hybrid architecture
    try:
        logger.info("[INIT] Fetching map data for cache...")
        map_data = await PathFinder.fetch_map_data(http_client, MAP_SERVICE_URL)
        pathfinder = PathFinder(map_data)
        session_manager = RouteSessionManager(pathfinder)
        logger.info(f"[INIT] Map cached: {len(pathfinder.nodes)} nodes")
    except Exception as e:
        logger.error(f"[INIT] Failed to load map data: {e}")
    
    # Start MQTT handler
    try:
        mqtt_handler = MQTTRoutingHandler(
            client_broker=CLIENT_BROKER,
            client_port=CLIENT_PORT
        )
        
        # Set callback handlers
        if session_manager:
            mqtt_handler.on_heartbeat = session_manager.handle_heartbeat
            mqtt_handler.on_waypoint = session_manager.handle_waypoint
            mqtt_handler.on_route_cancel = session_manager.handle_cancellation
        
        # Register wait time update handler
        mqtt_handler.on_waittime_update = handle_waittime_update
        
        # Register congestion update handler
        mqtt_handler.on_congestion_update = handle_congestion_update
        
        mqtt_handler.start()
        logger.info("[INIT] MQTT Handler started")
    except Exception as e:
        logger.warning(f"[INIT] MQTT handler failed to start: {e}")
        logger.warning("[INIT] Continuing without real-time routing updates...")
    
    # Start cleanup task
    cleanup_task = asyncio.create_task(cleanup_sessions())
    
    yield
    
    # Shutdown
    logger.info("Shutting down Routing Service...")
    
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
    
    if mqtt_handler:
        mqtt_handler.stop()
        logger.info("MQTT Handler stopped")
    
    await http_client.aclose()

app = FastAPI(
    title="Routing Service Fanapp",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware (allows Flutter web app to make requests)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
            
            # Get queue wait time for this POI from the MQTT cache (populated by WaitTime Service)
            wait_time = waittime_cache.get(request.destination_id)
        
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
        
        # 4. Calculate path (Internal logic + Dynamic Congestion + Wait Times)
        path_ids, total_cost = pathfinder.find_path(
            start_node_id,
            end_node_id,
            congestion_data,
            avoid_stairs=request.avoid_stairs,
            waittime_data=waittime_cache
        )
        
        if not path_ids:
            raise HTTPException(status_code=404, detail="No path found to destination")
        
        # 5. Build response
        path_nodes = []
        cumulative_distance = 0
        waypoints = []  # Track important decision points
        
        for i, node_id in enumerate(path_ids):
            node = pathfinder.nodes[node_id]
            
            if i > 0:
                prev_node = pathfinder.nodes[path_ids[i-1]]
                cumulative_distance += pathfinder.calculate_distance(
                    prev_node['x'], prev_node['y'],
                    node['x'], node['y']
                )
            
            # Identify waypoints (nodes with multiple exits or level changes)
            is_waypoint = False
            if i > 0 and i < len(path_ids) - 1:
                # Check if this node has multiple outgoing edges
                neighbors = pathfinder.graph.get(node_id, [])
                if len(neighbors) > 2:  # Junction point
                    is_waypoint = True
                    waypoints.append(node_id)
                # Check if level changes
                elif node['level'] != pathfinder.nodes[path_ids[i-1]]['level']:
                    is_waypoint = True
                    waypoints.append(node_id)
            
            path_nodes.append(PathNode(
                node_id=node_id,
                x=node['x'],
                y=node['y'],
                level=node['level'],
                distance_from_start=cumulative_distance,
                estimated_time=cumulative_distance / 1.4,
                is_waypoint=is_waypoint
            ))
        
        # Calculate average congestion
        congestion_map = {c.get('cell_id', c.get('id')): c['congestion_level'] for c in congestion_data.get('cells', [])}
        avg_congestion = sum(congestion_map.get(nid, 0.0) for nid in path_ids) / len(path_ids) if path_ids else 1.0
        
        # Create session if ticket_id provided
        session_id = None
        mqtt_topic = None
        if request.ticket_id and session_manager:
            session = session_manager.create_session(
                ticket_id=request.ticket_id,
                start_node=start_node_id,
                end_node=end_node_id,
                destination_type=request.destination_type,
                destination_id=request.destination_id,
                route=path_ids,
                total_cost=total_cost,
                avoid_stairs=request.avoid_stairs
            )
            session_id = session.session_id
            mqtt_topic = f"stadium/services/routing/{session_id}"
        
        return RouteResponse(
            path=path_nodes,
            total_distance=cumulative_distance,
            estimated_time=cumulative_distance / 1.4 + (wait_time or 0) * 60,
            congestion_level=avg_congestion,
            wait_time=wait_time,
            warnings=["High congestion"] if avg_congestion > 0.7 else [],
            session_id=session_id,
            mqtt_topic=mqtt_topic,
            waypoints=waypoints
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