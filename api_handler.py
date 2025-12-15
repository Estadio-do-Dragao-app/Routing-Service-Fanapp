from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List
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

# Active emergency closures (set of tile IDs that cannot be traversed)
active_closures: set = set()

# Emergency exits cache (list of node IDs for emergency exits)
emergency_exits: List[str] = []


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
        logger.info(f"[WAITTIME] Updated cache: {poi_id} = {wait_minutes} min (old={old_wait})")
        
        # Trigger rerouting check if wait time changed significantly (> 2 min difference)
        diff = abs(wait_minutes - old_wait)
        has_deps = session_manager and pathfinder
        if diff > 2 and has_deps:
            logger.info(f"[WAITTIME] Triggering reroute check for {poi_id} (diff={diff:.1f} min)")
            check_reroutes_for_waittime_change(poi_id, wait_minutes)
        else:
            logger.debug(f"[WAITTIME] No reroute check: diff={diff:.1f}, has_deps={has_deps}")
            
    except Exception as e:
        logger.error(f"[WAITTIME] Failed to process update for {poi_id}: {e}")


def get_poi_category(poi_id: str) -> str:
    """Extract category from POI ID (e.g., 'Food-Norte-1' -> 'Food')"""
    if '-' in poi_id:
        return poi_id.split('-')[0]
    return poi_id


async def find_alternative_pois(poi_id: str, poi_category: str) -> List[dict]:
    """Find alternative POIs of the same category with lower wait times"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{MAP_SERVICE_URL}/pois", timeout=5.0)
            if response.status_code != 200:
                return []
            
            all_pois = response.json()
            alternatives = []
            
            for poi in all_pois:
                # Check if same category but different POI
                if poi['id'] != poi_id and poi['id'].startswith(poi_category + '-'):
                    wait_time = waittime_cache.get(poi['id'], 0)
                    alternatives.append({
                        'id': poi['id'],
                        'x': poi['x'],
                        'y': poi['y'],
                        'level': poi['level'],
                        'wait_time': wait_time
                    })
            
            # Sort by wait time (ascending) but return ALL alternatives
            # The caller will calculate actual travel distance for each
            alternatives.sort(key=lambda x: x['wait_time'])
            return alternatives  # Return all alternatives for proximity calculation
            
    except Exception as e:
        logger.error(f"[REROUTE] Failed to find alternative POIs: {e}")
        return []


def check_reroutes_for_waittime_change(poi_id: str, new_wait_minutes: float):
    """Check if active sessions need rerouting due to wait time change"""
    import asyncio
    
    if not session_manager or not pathfinder or not mqtt_handler:
        logger.warning(f"[REROUTE] Missing dependencies: session_manager={bool(session_manager)}, pathfinder={bool(pathfinder)}, mqtt={bool(mqtt_handler)}")
        return
    
    active_sessions = session_manager.get_active_sessions()
    logger.info(f"[REROUTE] Checking {len(active_sessions)} active sessions for POI {poi_id}")
    
    # Debug: log all destination_ids
    for s in active_sessions:
        logger.info(f"[REROUTE] DEBUG: Session {s.session_id} destination_id='{s.destination_id}' destination_type='{s.destination_type}'")
    
    # Get POI category for finding alternatives
    poi_category = get_poi_category(poi_id)
    
    for session in active_sessions:
        # Only check sessions going to this POI (use destination_id, not end_node)
        # end_node is a node ID (e.g., N124), destination_id is the POI ID (e.g., WC-Norte-1)
        if session.destination_id != poi_id:
            continue
        
        logger.info(f"[REROUTE] Session {session.session_id} is going to {poi_id}")
        
        # Estimate current position
        estimated_node, confidence = session.estimate_current_position(pathfinder)
        logger.info(f"[REROUTE] Session {session.session_id}: position confidence={confidence:.2f}, node={estimated_node}")
        
        # Only recalculate if we have good position confidence
        if confidence < 0.5:
            logger.info(f"[REROUTE] Session {session.session_id}: skipping due to low confidence ({confidence:.2f})")
            continue
        
        try:
            # Find alternative POIs with lower wait time
            loop = asyncio.new_event_loop()
            alternatives = loop.run_until_complete(find_alternative_pois(poi_id, poi_category))
            loop.close()
            
            if not alternatives:
                logger.info(f"[REROUTE] Session {session.session_id}: no alternative POIs found")
                continue
            
            # Find best alternative (lowest total cost = travel + wait)
            best_alt = None
            best_cost = float('inf')
            best_route = None
            
            for alt in alternatives:
                alt_node = pathfinder.find_nearest_node(alt['x'], alt['y'], alt['level'])
                if not alt_node:
                    continue
                
                new_route, travel_cost = pathfinder.find_path(
                    estimated_node,
                    alt_node,
                    {"cells": []},
                    avoid_stairs=session.avoid_stairs,
                    waittime_data=waittime_cache
                )
                
                if new_route:
                    # Total cost includes travel + wait time at destination
                    total_cost = travel_cost + (alt['wait_time'] * 60)  # Convert wait to seconds
                    if total_cost < best_cost:
                        best_cost = total_cost
                        best_alt = alt
                        best_route = new_route
            
            if not best_alt:
                logger.info(f"[REROUTE] Session {session.session_id}: no viable alternatives found")
                continue
            
            # Compare with current destination cost
            current_wait = waittime_cache.get(poi_id, 0) * 60  # Wait in seconds
            current_cost = session.total_cost + current_wait
            
            improvement = (current_cost - best_cost) / current_cost if current_cost > 0 else 0
            logger.info(f"[REROUTE] Session {session.session_id}: alternative {best_alt['id']} cost={best_cost:.0f} vs current={current_cost:.0f} (improvement={improvement*100:.1f}%)")
            
            # Suggest reroute if improvement > 20%
            if improvement > 0.20:
                time_saved = current_cost - best_cost
                suggestion = {
                    "type": "reroute_suggestion",
                    "session_id": session.session_id,
                    "reason": f"Wait time at {poi_id} is {new_wait_minutes:.0f}min. {best_alt['id']} has {best_alt['wait_time']:.0f}min wait.",
                    "confidence": round(confidence, 2),
                    "current_estimate_node": estimated_node,
                    "new_route": best_route,
                    "new_destination": best_alt['id'],
                    "category": poi_category,  # Category for nearest_category lookup (e.g., "WC", "Food")
                    "improvement": {
                        "cost_reduction": round(improvement * 100, 1),
                        "time_saved_seconds": round(time_saved, 0),
                        "time_saved_display": f"{int(time_saved // 60)}min {int(time_saved % 60)}s"
                    }
                }
                
                mqtt_handler.publish_route_update(session.session_id, suggestion)
                logger.info(f"[REROUTE] Suggested alternative {best_alt['id']} for session {session.session_id}")
            else:
                logger.info(f"[REROUTE] Session {session.session_id}: improvement not significant enough")
                
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


def handle_emergency_alert(payload: dict):
    """Handle emergency alerts from Alert-Service and trigger evacuation routes"""
    global active_closures
    
    try:
        alert_type = payload.get('alert_type', 'unknown').upper()
        affected_areas = payload.get('affected_areas', [])  # Tile IDs
        severity = payload.get('severity', 3)
        level = payload.get('level', 0)  # Stadium level affected
        
        logger.info(f"[EMERGENCY] Processing {alert_type} alert - Severity: {severity}, Affected tiles: {len(affected_areas)}")
        
        # For FIRE or high-severity alerts, resolve tiles to nodes and add to closures
        if alert_type in ['FIRE', 'EVACUATION', 'SECURITY'] or severity >= 4:
            if affected_areas:
                # Resolve tile IDs to node IDs via Map-Service
                resolved_nodes = resolve_tiles_to_nodes(affected_areas)
                for node_id in resolved_nodes:
                    active_closures.add(node_id)
                logger.info(f"[EMERGENCY] Resolved {len(affected_areas)} tiles to {len(resolved_nodes)} nodes. Total active closures: {len(active_closures)}")
        
        # Trigger evacuation routes for all active sessions
        if alert_type in ['FIRE', 'EVACUATION'] or severity >= 5:
            trigger_evacuation_routes(level)
            
    except Exception as e:
        logger.error(f"[EMERGENCY] Failed to process alert: {e}")


def resolve_tiles_to_nodes(tile_ids: list) -> set:
    """Call Map-Service to resolve tile IDs to node IDs"""
    import requests
    
    map_service_url = os.getenv('MAP_SERVICE_URL', 'http://mapservice:8000')
    
    try:
        response = requests.post(
            f"{map_service_url}/maps/grid/tiles/nodes",
            json=tile_ids,
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        node_ids = set(data.get('node_ids', []))
        logger.info(f"[EMERGENCY] Resolved tiles via Map-Service: {len(node_ids)} nodes from {data.get('tile_count', 0)} tiles")
        return node_ids
    except Exception as e:
        logger.error(f"[EMERGENCY] Failed to resolve tiles to nodes: {e}")
        return set()



def trigger_evacuation_routes(alert_level: int = None):
    """Calculate and send evacuation routes to all active sessions"""
    if not session_manager or not pathfinder or not mqtt_handler:
        logger.warning("[EMERGENCY] Cannot trigger evacuation - services not initialized")
        return
    
    active_sessions = session_manager.get_active_sessions()
    logger.info(f"[EMERGENCY] Triggering evacuation for {len(active_sessions)} active sessions")
    
    for session in active_sessions:
        try:
            # Estimate current position
            estimated_node, confidence = session.estimate_current_position(pathfinder)
            
            if not estimated_node or confidence < 0.3:
                # Use last known waypoint or start node
                estimated_node = session.current_waypoint or session.start_node
            
            if not estimated_node:
                continue
            
            # Find route to nearest emergency exit
            # Get node info to know the level
            node_info = pathfinder.nodes.get(estimated_node, {})
            user_level = node_info.get('level', 0)
            
            # Find emergency exit nodes (filter by level if possible)
            exit_nodes = [
                nid for nid, node in pathfinder.nodes.items()
                if node.get('type') == 'emergency_exit'
            ]
            
            if not exit_nodes:
                # Fallback: use gate nodes as exits
                exit_nodes = [
                    nid for nid, node in pathfinder.nodes.items()
                    if node.get('type') == 'gate'
                ]
            
            if not exit_nodes:
                logger.warning(f"[EMERGENCY] No exit nodes found for session {session.session_id}")
                continue
            
            # Build congestion data with closures applied
            congestion_data = {"cells": [
                {"cell_id": cid, "congestion_level": level}
                for cid, level in congestion_cache.items()
            ]}
            
            # Find path to nearest valid exit (avoiding closures)
            best_route = None
            best_cost = float('inf')
            best_exit = None
            
            logger.info(f"[EMERGENCY] Testing {len(exit_nodes)} exit nodes for session from {estimated_node}")
            exit_costs = {}
            
            for exit_node in exit_nodes:
                try:
                    route, cost = pathfinder.find_path(
                        estimated_node,
                        exit_node,
                        congestion_data,
                        avoid_stairs=session.avoid_stairs,
                        waittime_data=waittime_cache,
                        blocked_nodes=active_closures
                    )
                    
                    if route:
                        exit_costs[exit_node] = cost
                        if cost < best_cost:
                            best_route = route
                            best_cost = cost
                            best_exit = exit_node
                        
                except Exception as e:
                    logger.debug(f"[EMERGENCY] No route to exit {exit_node}: {e}")
                    continue
            
            # Log all tested exits for debugging
            sorted_exits = sorted(exit_costs.items(), key=lambda x: x[1])
            logger.info(f"[EMERGENCY] Exit costs (top 5): {sorted_exits[:5]}")
            
            if best_route:
                # Create evacuation route update
                evacuation_update = {
                    "type": "evacuation",
                    "reason": "Emergency evacuation - follow this route to the nearest exit",
                    "route": best_route,
                    "cost": best_cost,
                    "destination": best_exit,
                    "priority": "high",
                    "replaces_current_route": True
                }
                
                mqtt_handler.publish_route_update(session.session_id, evacuation_update)
                logger.info(f"[EMERGENCY] Sent evacuation route to session {session.session_id} -> exit {best_exit}")
            else:
                logger.warning(f"[EMERGENCY] No evacuation route found for session {session.session_id}")
                
        except Exception as e:
            logger.error(f"[EMERGENCY] Failed to calculate evacuation for {session.session_id}: {e}")

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
        
        # Register emergency alert handler
        mqtt_handler.on_alert = handle_emergency_alert
        
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


class AlertRequest(BaseModel):
    alert_type: str
    severity: int
    message: Optional[str] = "Emergency Alert"
    affected_areas: Optional[List[str]] = []
    level: Optional[int] = 0

@app.post("/api/alerts")
async def trigger_alert(request: AlertRequest):
    """
    Manually trigger an emergency alert via HTTP
    """
    logger.info(f"[API] Received manual alert trigger: {request.alert_type}")
    handle_emergency_alert(request.dict())
    return {"status": "processed", "alert_type": request.alert_type}

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
        
        elif request.destination_type == "nearest_category":
            # Find FASTEST POI of a category from user's position
            # Uses actual pathfinding costs (congestion + wait + travel), not just distance
            category = request.destination_id  # e.g., "WC", "Food"
            
            # Get all POIs
            pois_response = await http_client.get(f"{MAP_SERVICE_URL}/pois")
            if pois_response.status_code != 200:
                raise HTTPException(status_code=404, detail="Could not fetch POIs")
            
            all_pois = pois_response.json()
            
            # Filter POIs by category
            category_pois = [p for p in all_pois if p['id'].startswith(category + '-')]
            if not category_pois:
                raise HTTPException(status_code=404, detail=f"No POIs found for category {category}")
            
            # Calculate ACTUAL route cost to each POI (travel + congestion + wait)
            best_poi = None
            best_cost = float('inf')
            best_poi_node = None
            
            for poi in category_pois:
                # Find nearest node to this POI
                poi_node = pathfinder.find_nearest_node(poi['x'], poi['y'], poi['level'])
                if not poi_node:
                    continue
                
                # Calculate actual route cost using pathfinder (includes congestion)
                route, travel_cost = pathfinder.find_path(
                    start_node_id,
                    poi_node,
                    congestion_data,
                    avoid_stairs=request.avoid_stairs,
                    waittime_data=waittime_cache,
                    blocked_nodes=active_closures
                )
                
                if not route:
                    continue
                
                # Add wait time at this POI (converted to seconds)
                poi_wait = waittime_cache.get(poi['id'], 0) * 60
                total_cost = travel_cost + poi_wait
                
                logger.debug(f"[ROUTE] {poi['id']}: travel={travel_cost:.0f}s + wait={poi_wait:.0f}s = total={total_cost:.0f}s")
                
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_poi = poi
                    best_poi_node = poi_node
            
            if not best_poi:
                raise HTTPException(status_code=404, detail=f"No accessible POI found in category {category}")
            
            logger.info(f"[ROUTE] Fastest {category}: {best_poi['id']} total_cost={best_cost:.0f}s (wait={waittime_cache.get(best_poi['id'], 0):.0f}m)")
            
            end_node_id = best_poi_node
            
            # Get queue wait time for this POI
            wait_time = waittime_cache.get(best_poi['id'])
            
            # Store the actual POI ID for the session (so destination_id is correct)
            request.destination_id = best_poi['id']
        
        if not end_node_id:
            raise HTTPException(status_code=404, detail="Destination node not found")
        
        # 4. Calculate path (Internal logic + Dynamic Congestion + Wait Times)
        # Apply active closures (e.g. fire zones)
        path_ids, total_cost = pathfinder.find_path(
            start_node_id,
            end_node_id,
            congestion_data,
            avoid_stairs=request.avoid_stairs,
            waittime_data=waittime_cache,
            blocked_nodes=active_closures  # Use global active closures
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
        if session_manager:
            # Use ticket_id if provided, otherwise generate a fallback session ID
            import uuid
            effective_ticket_id = request.ticket_id or f"anon-{uuid.uuid4().hex[:8]}"
            session = session_manager.create_session(
                ticket_id=effective_ticket_id,
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
            logger.info(f"[SESSION] Created session {session_id} for route to {end_node_id}")
        
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