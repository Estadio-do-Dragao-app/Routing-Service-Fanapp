from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional, Dict, List, Set, Annotated
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import httpx
import os
import asyncio
import logging
import math
import secrets
from dotenv import load_dotenv

from models import RouteRequest, RouteResponse, PathNode, Coordinates
from pathFinding import PathFinder
from route_manager import RouteSessionManager
from mqtt_handler import MQTTRoutingHandler
from constants import WALKING_SPEED

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAP_SERVICE_URL = os.getenv("MAP_SERVICE_URL", "http://localhost:8000")
CONGESTION_SERVICE_URL = os.getenv("CONGESTION_SERVICE_URL", "http://localhost:8004")
CLIENT_BROKER = os.getenv("CLIENT_BROKER", "localhost")
CLIENT_PORT = int(os.getenv("CLIENT_PORT", 1884))
API_KEY = os.getenv("API_KEY", "dragao_secret_key_2026")

@dataclass
class ServiceState:
    """Centralized state for the Routing Service"""
    http_client: Optional[httpx.AsyncClient] = None
    pathfinder: Optional[PathFinder] = None
    session_manager: Optional[RouteSessionManager] = None
    mqtt_handler: Optional[MQTTRoutingHandler] = None
    cleanup_task: Optional[asyncio.Task] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    
    # Caches
    waittime_cache: Dict[str, float] = field(default_factory=dict)
    congestion_cache: Dict[str, float] = field(default_factory=dict)
    active_closures: Set[str] = field(default_factory=set)
    poi_cache: List[dict] = field(default_factory=list)
    
    # Thread safety
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    background_tasks: Set[asyncio.Task] = field(default_factory=set)

# Global service state
state = ServiceState()




def get_poi_category(poi_id: str) -> str:
    """
    Extract category from POI ID or metadata.
    Prioritizes the 'type' field from Map Service if available.
    """
    # Look up POI in cache to find its type
    for poi in state.poi_cache:
        if poi['id'] == poi_id:
            return poi.get('type', poi.get('category', 'POI'))
            
    # Fallback to ID-based detection
    if '-' in poi_id:
        return poi_id.split('-')[0]
    if '_' in poi_id:
        return poi_id.split('_')[0]
    return 'POI'


def _is_poi_match(poi: dict, poi_id: str, poi_category: str) -> bool:
    """Helper to check if a POI matches category/type and is not the original POI"""
    if poi['id'] == poi_id:
        return False
    p_cat = poi.get('type', poi.get('category'))
    cat_match = str(p_cat).lower() == str(poi_category).lower()
    prefix_match = poi['id'].lower().startswith(str(poi_category).lower() + '-')
    return cat_match or prefix_match


async def find_alternative_pois(poi_id: str, poi_category: str) -> List[dict]:
    """Find alternative POIs of the same category with lower wait times"""
    try:
        alternatives = []
        for poi in state.poi_cache:
            if _is_poi_match(poi, poi_id, poi_category):
                wait_time = state.waittime_cache.get(poi['id'], 0)
                alternatives.append({
                    'id': poi['id'],
                    'x': poi['x'],
                    'y': poi['y'],
                    'level': poi['level'],
                    'wait_time': wait_time
                })
        
        # If cache is empty, fallback to API
        if not alternatives and state.http_client:
            resp = await state.http_client.get(f"{MAP_SERVICE_URL}/pois", timeout=5.0)
            if resp.status_code == 200:
                for poi in resp.json():
                    if _is_poi_match(poi, poi_id, poi_category):
                        wait_time = state.waittime_cache.get(poi['id'], 0)
                        alternatives.append({
                            'id': poi['id'],
                            'x': poi['x'],
                            'y': poi['y'],
                            'level': poi['level'],
                            'wait_time': wait_time
                        })
        
        alternatives.sort(key=lambda x: x['wait_time'])
        return alternatives
            
    except Exception as e:
        logger.exception("[REROUTE] Failed to find alternative POIs")
        return []

async def handle_waittime_update_async(poi_id: str, payload: dict):
    """Handle wait time updates (Async for thread safety)"""
    async with state.lock:
        try:
            # Support multiple payload formats
            new_wait_minutes = float(payload.get('minutes', payload.get('wait_minutes', payload.get('wait_time', 0))))
            
            old_wait = state.waittime_cache.get(poi_id, 0)
            state.waittime_cache[poi_id] = new_wait_minutes
            
            # Trigger rerouting check if wait time changed significantly (> 2 min)
            if abs(new_wait_minutes - old_wait) > 2.0 and state.session_manager:
                logger.info(f"[WAITTIME] Triggering reroute check for {poi_id} (diff={abs(new_wait_minutes - old_wait):.1f} min)")
                # Already in an async context, no need for run_coroutine_threadsafe
                await check_reroutes_for_waittime_change(poi_id, new_wait_minutes)
                
        except Exception as e:
            logger.exception("[WAITTIME] Failed to process update for {poi_id}")

def handle_waittime_update(poi_id: str, payload: dict):
    """Thread-safe bridge for MQTT callback"""
    if state.loop:
        asyncio.run_coroutine_threadsafe(handle_waittime_update_async(poi_id, payload), state.loop)



def _find_best_alternative(
    session,
    estimated_node: str,
    est_pos: tuple,
    alt_with_nodes: list,
    congestion_data: dict
) -> tuple:
    """Find the best alternative POI for the session."""
    best_alt = None
    best_cost_seconds = float('inf')
    best_route = None
    
    for alt, alt_node in alt_with_nodes:
        if alt['id'] == session.destination_id:
            continue
            
        new_route, path_distance = state.pathfinder.find_path(
            estimated_node,
            alt_node,
            congestion_data,
            avoid_stairs=session.avoid_stairs,
            waittime_data=state.waittime_cache
        )
        
        if new_route:
            start_node = state.pathfinder.nodes[new_route[0]]
            offset_dist = state.pathfinder.calculate_distance(est_pos[0], est_pos[1], start_node['x'], start_node['y'])
            
            travel_time = (path_distance + offset_dist) / WALKING_SPEED
            service_time = alt['wait_time'] * 60
            total_time = travel_time + service_time
            
            if total_time < best_cost_seconds:
                best_cost_seconds = total_time
                best_alt = alt
                best_route = new_route
                
    return best_alt, best_route, best_cost_seconds


def _evaluate_session_for_alternative_poi(
    session,
    poi_id: str,
    poi_category: str,
    alt_with_nodes: list,
    congestion_data: dict
):
    """Evaluate if a single session should be rerouted to an alternative POI."""
    if session.destination_type != 'poi' or session.destination_id != poi_id:
        return
        
    est_pos, confidence = session.estimate_current_position(state.pathfinder)
    estimated_node = state.pathfinder.find_nearest_node(est_pos[0], est_pos[1], est_pos[2])
    
    if not estimated_node or confidence < 0.4:
        return
        
    try:
        best_alt, best_route, best_cost_seconds = _find_best_alternative(
            session, estimated_node, est_pos, alt_with_nodes, congestion_data
        )
        
        if not best_alt:
            return
            
        _, current_dist = state.pathfinder.find_path(
            estimated_node,
            session.end_node,
            congestion_data,
            avoid_stairs=session.avoid_stairs,
            waittime_data=state.waittime_cache
        )
        
        start_node = state.pathfinder.nodes[estimated_node]
        offset_dist = state.pathfinder.calculate_distance(est_pos[0], est_pos[1], start_node['x'], start_node['y'])
        
        current_wait_time = state.waittime_cache.get(poi_id, 0) * 60
        current_total_time = ((current_dist + offset_dist) / WALKING_SPEED) + current_wait_time
        
        if not math.isfinite(current_total_time) or current_total_time <= 0:
            improvement = 1.0 if math.isfinite(best_cost_seconds) else 0
            time_saved = 0
        else:
            improvement = (current_total_time - best_cost_seconds) / current_total_time
            time_saved = current_total_time - best_cost_seconds
            
        if improvement > 0.25 and math.isfinite(time_saved):
            if getattr(session, 'last_suggested_id', None) == best_alt['id']:
                return
                
            session.last_suggested_id = best_alt['id']
            
            suggestion = {
                "type": "reroute_suggestion",
                "session_id": session.session_id,
                "reason": f"Wait time at {poi_id} is {state.waittime_cache.get(poi_id, 0):.0f}min. {best_alt['id']} has {best_alt['wait_time']:.0f}min wait.",
                "confidence": round(confidence, 2),
                "current_estimate_node": estimated_node,
                "new_route": best_route,
                "new_destination": best_alt['id'],
                "category": poi_category,
                "improvement": {
                    "cost_reduction": round(improvement * 100, 1),
                    "time_saved_seconds": round(time_saved, 0),
                    "time_saved_display": f"{int(time_saved // 60)}min {int(time_saved % 60)}s"
                }
            }
            
            state.mqtt_handler.publish_route_update(session.session_id, suggestion)
            logger.info(f"[REROUTE] Suggested alternative {best_alt['id']} for session {session.session_id} (Saved {int(time_saved)}s)")
            
    except Exception as e:
        logger.exception("[REROUTE] Failed to check reroute for {session.session_id}")


async def check_reroutes_for_waittime_change(poi_id: str, new_wait_minutes: float):
    """Check if active sessions need rerouting due to wait time change (Async)"""
    if not state.session_manager or not state.pathfinder or not state.mqtt_handler:
        return
    
    active_sessions = state.session_manager.get_active_sessions()
    if not active_sessions:
        return
        
    logger.info(f"[REROUTE] Checking {len(active_sessions)} active sessions for POI {poi_id} (new wait time: {new_wait_minutes} min)")
    
    poi_category = get_poi_category(poi_id)
    alternatives = await find_alternative_pois(poi_id, poi_category)
    if not alternatives:
        logger.info(f"[REROUTE] No alternative POIs found for category {poi_category}")
        return

    alt_with_nodes = []
    for alt in alternatives:
        alt_node = state.pathfinder.find_nearest_node(alt['x'], alt['y'], alt['level'])
        if alt_node:
            alt_with_nodes.append((alt, alt_node))

    congestion_data = {
        "cells": [
            {"cell_id": cid, "congestion_level": level}
            for cid, level in state.congestion_cache.items()
        ]
    }

    for session in active_sessions:
        _evaluate_session_for_alternative_poi(
            session, poi_id, poi_category, alt_with_nodes, congestion_data
        )


async def check_reroutes_for_congestion_change(cell_id: str):
    """Check if active sessions need rerouting due to congestion change (Async)"""
    if not state.session_manager or not state.pathfinder or not state.mqtt_handler:
        return
    
    async with state.lock:
        active_sessions = state.session_manager.get_active_sessions()
        
        for session in active_sessions:
            # Estimate current exact coordinates
            est_pos, confidence = session.estimate_current_position(state.pathfinder)
            
            # Snap estimated position to nearest node for A*
            estimated_node = state.pathfinder.find_nearest_node(est_pos[0], est_pos[1], est_pos[2])
            
            if not estimated_node or confidence < 0.4:
                continue
            
            try:
                # Build congestion data for pathfinding from cache
                congestion_data = {"cells": [
                    {"cell_id": cid, "congestion_level": level}
                    for cid, level in state.congestion_cache.items()
                ]}
                
                # Recalculate route from estimated position with current congestion
                new_route, new_dist = state.pathfinder.find_path(
                    estimated_node,
                    session.end_node,
                    congestion_data,
                    avoid_stairs=session.avoid_stairs,
                    waittime_data=state.waittime_cache
                )
                
                if not new_route:
                    continue
                
                # FIX: Precision (distance from actual est_pos to start of route)
                start_node = state.pathfinder.nodes[new_route[0]]
                offset_dist = state.pathfinder.calculate_distance(est_pos[0], est_pos[1], start_node['x'], start_node['y'])
                
                # Unit consistency (Seconds)
                new_cost_seconds = (new_dist + offset_dist) / WALKING_SPEED
                if session.destination_type == 'poi':
                    new_cost_seconds += state.waittime_cache.get(session.destination_id, 0) * 60

                # Check if rerouting is beneficial
                suggestion = state.session_manager.should_reroute(
                    session, new_route, new_cost_seconds,
                    reason=f"Congestion changed in area (cell {cell_id})"
                )
                
                if suggestion:
                    state.mqtt_handler.publish_route_update(session.session_id, suggestion)
                    logger.info(f"[REROUTE] Suggested new route for session {session.session_id} due to congestion")
                    
            except Exception as e:
                logger.exception("[REROUTE] Failed to check congestion reroute for {session.session_id}")

async def handle_congestion_update_async(payload: dict):
    """Handle congestion updates (Async for thread safety)"""
    async with state.lock:
        try:
            cell_id = payload.get('cell_id')
            new_level = payload.get('congestion_level', 0)
            
            if not cell_id:
                return
            
            old_level = state.congestion_cache.get(cell_id, 0)
            state.congestion_cache[cell_id] = float(new_level)
            
            # Trigger rerouting check if congestion changed significantly (> 20% difference)
            level_diff = abs(new_level - old_level)
            if level_diff > 0.2 and state.session_manager and state.pathfinder:
                logger.info(f"[CONGESTION] Cell {cell_id} changed: {old_level:.2f} -> {new_level:.2f}")
                # Schedule rerouting check on event loop
                task = asyncio.create_task(check_reroutes_for_congestion_change(cell_id))
                state.background_tasks.add(task)
                task.add_done_callback(state.background_tasks.discard)
                
        except Exception as e:
            logger.exception("[CONGESTION] Failed to process update")


def handle_congestion_update(payload: dict):
    """Thread-safe bridge for MQTT callback"""
    if state.loop:
        asyncio.run_coroutine_threadsafe(handle_congestion_update_async(payload), state.loop)


def _estimate_session_node(session) -> Optional[str]:
    """Estimate snapped node for session (handles GPS decays and checkpoint fallbacks)"""
    est_pos, confidence = session.estimate_current_position(state.pathfinder)
    estimated_node = state.pathfinder.find_nearest_node(est_pos[0], est_pos[1], est_pos[2])
    
    if not estimated_node or confidence < 0.2:
        if session.last_checkpoint and session.last_checkpoint.node_id:
            estimated_node = session.last_checkpoint.node_id
        else:
            estimated_node = session.current_waypoint or session.start_node
            
    return estimated_node


def _calculate_evacuation_for_session(session, exit_nodes: list, congestion_data: dict):
    """Calculate and publish evacuation route for a single active session."""
    try:
        estimated_node = _estimate_session_node(session)
        if not estimated_node:
            return
        
        # Find path to nearest valid exit (avoiding closures)
        best_route = None
        best_cost = float('inf')
        best_exit = None
        
        for exit_node in exit_nodes:
            try:
                route, cost = state.pathfinder.find_path(
                    estimated_node,
                    exit_node,
                    congestion_data,
                    avoid_stairs=session.avoid_stairs,
                    waittime_data=state.waittime_cache,
                    blocked_nodes=state.active_closures
                )
                
                if route and cost < best_cost:
                    best_route = route
                    best_cost = cost
                    best_exit = exit_node
            except Exception:
                continue
        
        if best_route:
            evacuation_update = {
                "type": "evacuation",
                "reason": "Emergency evacuation - follow this route to the nearest exit",
                "route": best_route,
                "cost": round(best_cost / WALKING_SPEED, 0),
                "destination": best_exit,
                "priority": "CRITICAL",
                "replaces_current_route": True
            }
            state.mqtt_handler.publish_route_update(session.session_id, evacuation_update, priority="CRITICAL", qos=2)
            logger.info(f"[EMERGENCY] Sent evacuation route to session {session.session_id} -> exit {best_exit}")
    except Exception as e:
        logger.exception("[EMERGENCY] Failed to calculate evacuation for {session.session_id}")


async def trigger_evacuation_routes_async():
    """Calculate and send evacuation routes to all active sessions (Async & Thread-Safe)"""
    if not state.session_manager or not state.pathfinder or not state.mqtt_handler:
        logger.warning("[EMERGENCY] Cannot trigger evacuation - services not initialized")
        return
    
    async with state.lock:
        active_sessions = state.session_manager.get_active_sessions()
        logger.info(f"[EMERGENCY] Triggering evacuation for {len(active_sessions)} active sessions")
        
        # Pre-identify exit nodes
        exit_nodes = [
            nid for nid, node in state.pathfinder.nodes.items()
            if node.get('type') == 'emergency_exit'
        ]
        if not exit_nodes:
            # Fallback: use gate nodes as exits
            exit_nodes = [
                nid for nid, node in state.pathfinder.nodes.items()
                if node.get('type') == 'gate'
            ]

        if not exit_nodes:
            logger.warning("[EMERGENCY] No exit nodes found in map")
            return

        # Build current congestion data
        congestion_data = {"cells": [
            {"cell_id": cid, "congestion_level": level}
            for cid, level in state.congestion_cache.items()
        ]}

        for session in active_sessions:
            _calculate_evacuation_for_session(session, exit_nodes, congestion_data)
                    
async def resolve_tiles_to_nodes(tile_ids: list) -> set:
    """Call Map-Service to resolve tile IDs to node IDs (Async)"""
    try:
        client = state.http_client or httpx.AsyncClient(timeout=10.0)
        response = await client.post(
            f"{MAP_SERVICE_URL}/maps/grid/tiles/nodes",
            json=tile_ids,
            timeout=5,
            headers=_get_api_headers()
        )
        response.raise_for_status()
        data = response.json()
        node_ids = set(data.get('node_ids', []))
        logger.info(f"[EMERGENCY] Resolved tiles via Map-Service: {len(node_ids)} nodes")
        return node_ids
    except Exception as e:
        logger.exception("[EMERGENCY] Failed to resolve tiles to nodes")
        return set()


async def process_emergency_closures(tile_ids: list):
    """Async wrapper to resolve tiles and update state closures"""
    resolved_nodes = await resolve_tiles_to_nodes(tile_ids)
    state.active_closures.update(resolved_nodes)
    logger.info(f"[EMERGENCY] Updated active closures. Total: {len(state.active_closures)}")

async def handle_emergency_alert_async(payload: dict):
    """Handle emergency alerts (Async for thread safety)"""
    try:
        alert_type = payload.get('alert_type', 'unknown').upper()
        affected_areas = payload.get('affected_areas', [])  # Tile IDs
        severity = payload.get('severity', 3)
        
        logger.info(f"[EMERGENCY] Processing {alert_type} alert - Severity: {severity}, Affected tiles: {len(affected_areas)}")
        
        # For FIRE or high-severity alerts, resolve tiles to nodes and add to closures
        if alert_type in ['FIRE', 'EVACUATION', 'SECURITY'] or severity >= 4:
            if affected_areas:
                await process_emergency_closures(affected_areas)
        
        # Trigger evacuation routes for all active sessions
        if alert_type in ['FIRE', 'EVACUATION'] or severity >= 5:
            await trigger_evacuation_routes_async()
            
    except Exception as e:
        logger.exception("[EMERGENCY] Failed to process alert")

def handle_emergency_alert(payload: dict):
    """Thread-safe bridge for MQTT callback"""
    if state.loop:
        asyncio.run_coroutine_threadsafe(handle_emergency_alert_async(payload), state.loop)

async def cleanup_sessions():
    """Periodically cleanup expired sessions"""
    while True:
        await asyncio.sleep(60)  # Every minute
        if state.session_manager:
            expired = state.session_manager.cleanup_expired_sessions()
            if expired and state.mqtt_handler:
                for session_id in expired:
                    # Notify client that session expired
                    state.mqtt_handler.publish_route_update(
                        session_id,
                        {"type": "session_expired", "reason": "timeout"}
                    )


async def sync_state(client: httpx.AsyncClient):
    """Fetch POIs and Congestion into the global caches (called once at startup and on map update)."""
    # Sync POIs (Both DB and OSM)
    try:
        headers = _get_api_headers()
        # 1. Standard POIs from DB
        resp = await client.get(f"{MAP_SERVICE_URL}/pois", headers=headers, timeout=10.0)
        standard_pois = resp.json() if resp.status_code == 200 else []
        
        # 2. Dynamic POIs from OSM
        resp_osm = await client.get(f"{MAP_SERVICE_URL}/pois/osm", headers=headers, timeout=35.0)
        osm_pois = resp_osm.json().get("pois", []) if resp_osm.status_code == 200 else []
        
        # Merge (prefer standard if ID conflicts)
        merged_pois = {p['id']: p for p in osm_pois}
        for p in standard_pois:
            merged_pois[p['id']] = p
            
        state.poi_cache = list(merged_pois.values())
        logger.info(f"[INIT] Event POIs cached: {len(state.poi_cache)}")
        
    except Exception as e:
        logger.warning(f"[INIT] POI sync failed: {e}")

    # Sync Congestion cells (initial full state)
    try:
        resp = await client.get(f"{CONGESTION_SERVICE_URL}/heatmap/stadium/cells", headers={"X-API-Key": API_KEY}, timeout=5.0)
        if resp.status_code == 200:
            cells = resp.json().get("cells", [])
            for cell in cells:
                cid = cell.get("cell_id", cell.get("id"))
                if cid:
                    state.congestion_cache[cid] = float(cell.get("congestion_level", 0.0))
            logger.info(f"[INIT] Congestion state synced: {len(state.congestion_cache)} cells")
    except Exception as e:
        logger.warning(f"[INIT] Congestion sync failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup resources on startup/shutdown"""
    logger.info("=" * 50)
    logger.info("Starting Event Routing Service...")
    logger.info("=" * 50)
    
    state.http_client = httpx.AsyncClient(timeout=10.0)
    state.loop = asyncio.get_running_loop()
    
    if not API_KEY:
        logger.error("[SECURITY] API_KEY environment variable is not set! Service will reject all authenticated requests.")
    
    # Initialize with empty data
    state.pathfinder = PathFinder({"nodes": [], "edges": []})
    state.session_manager = RouteSessionManager(state.pathfinder)
    
    # Pre-fetch map data with retries
    for i in range(10):
        try:
            logger.info(f"[INIT] Fetching map data (Attempt {i+1}/10)")
            map_data = await PathFinder.fetch_map_data(state.http_client, MAP_SERVICE_URL)
            state.pathfinder = PathFinder(map_data)
            state.session_manager.pathfinder = state.pathfinder
            logger.info(f"[INIT] Map cached: {len(state.pathfinder.nodes)} nodes")
            break
        except Exception as e:
            logger.warning(f"[INIT] Failed to load map data: {e}")
            await asyncio.sleep(5)
            
    # Sync POIs and Congestion
    await sync_state(state.http_client)
    
    # Start MQTT handler
    try:
        state.mqtt_handler = MQTTRoutingHandler(
            client_broker=CLIENT_BROKER,
            client_port=CLIENT_PORT
        )
        
        state.mqtt_handler.on_heartbeat = state.session_manager.handle_heartbeat
        state.mqtt_handler.on_waypoint = state.session_manager.handle_waypoint
        state.mqtt_handler.on_route_cancel = state.session_manager.handle_cancellation
        state.mqtt_handler.on_waittime_update = handle_waittime_update
        state.mqtt_handler.on_congestion_update = handle_congestion_update
        state.mqtt_handler.on_alert = handle_emergency_alert
        
        # Security: Verify that MQTT messages come from known active tickets
        state.mqtt_handler.verify_ticket_callback = lambda tid: tid in state.session_manager.ticket_sessions if state.session_manager else False
        
        state.mqtt_handler.start()
        logger.info("[INIT] MQTT Handler started")
    except Exception as e:
        logger.warning(f"[INIT] MQTT handler failed: {e}")
    
    state.cleanup_task = asyncio.create_task(cleanup_sessions())
    
    yield
    
    logger.info("Shutting down Event Routing Service...")
    if state.cleanup_task:
        state.cleanup_task.cancel()
    if state.mqtt_handler:
        state.mqtt_handler.stop()
    await state.http_client.aclose()

app = FastAPI(
    title="Routing Service Fanapp",
    version="1.0.0",
    lifespan=lifespan
)

from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)

# Add CORS middleware (allows Flutter web app to make requests)
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex="https?://.*",  # Flexible but safer than "*" with credentials
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _get_api_headers() -> dict:
    """Get standardized API headers with authorization."""
    return {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json"
    }


API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def get_api_key(api_key_value: Optional[str] = Security(api_key_header)):
    if api_key_value and secrets.compare_digest(api_key_value, API_KEY):
        return api_key_value
    raise HTTPException(
        status_code=401,
        detail="Unauthorized access - invalid or missing API key"
    )



class AlertRequest(BaseModel):
    alert_type: str
    severity: int
    message: Optional[str] = "Emergency Alert"
    affected_areas: Optional[List[str]] = []
    level: Optional[int] = 0

@app.post("/api/alerts")
async def trigger_alert(
    request: AlertRequest,
    api_key: Annotated[str, Depends(get_api_key)]
):
    """
    Manually trigger an emergency alert via HTTP
    """
    logger.info("[API] Received manual alert trigger")
    await handle_emergency_alert_async(request.dict())
    return {"status": "processed"}

@app.post(
    "/api/refresh_map",
    responses={
        500: {"description": "Map refresh failed"}
    }
)
async def refresh_map(api_key: Annotated[str, Depends(get_api_key)]):
    """Manually trigger a map data refresh from mapservice"""
    try:
        logger.info("[API] Manual map refresh triggered...")
        task = asyncio.create_task(_refresh_all_caches())
        state.background_tasks.add(task)

        def _log_refresh_task_result(done_task: asyncio.Task):
            try:
                done_task.result()
                logger.info("[REFRESH] Background refresh task completed")
            except asyncio.CancelledError:
                logger.warning("[REFRESH] Background refresh task cancelled")
            except Exception:
                logger.exception("[REFRESH] Background refresh task failed")
            finally:
                state.background_tasks.discard(done_task)

        task.add_done_callback(_log_refresh_task_result)
        return {
            "status": "accepted",
            "message": "Map refresh started",
            "nodes": len(state.pathfinder.nodes) if state.pathfinder else 0, 
            "pois": len(state.poi_cache)
        }
    except Exception as e:
        logger.exception("[API] Map refresh failed")
        raise HTTPException(status_code=500, detail=f"Map refresh failed: {str(e)}")



async def _refresh_all_caches():
    """Re-fetch map, POIs and congestion into RAM."""
    client = state.http_client or httpx.AsyncClient(timeout=60.0)
    map_data = None
    for attempt in range(1, 4):
        try:
            map_data = await PathFinder.fetch_map_data(client, MAP_SERVICE_URL)
            break
        except Exception as e:
            logger.warning(f"[REFRESH] Attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2)

    if map_data:
        state.pathfinder = PathFinder(map_data)
        if state.session_manager:
            state.session_manager.pathfinder = state.pathfinder
        logger.info(f"[REFRESH] Map refreshed: {len(state.pathfinder.nodes)} nodes")
        await sync_state(client)

@app.post(
    "/api/route",
    response_model=RouteResponse,
    responses={
        404: {"description": "No route/node/POI found"},
        500: {"description": "Internal route calculation error"},
        503: {"description": "Routing service not initialized"}
    }
)
async def calculate_route(
    request: RouteRequest,
    api_key: Annotated[str, Depends(get_api_key)]
):
    """
    Calculate optimal route from user coordinates to destination
    """
    try:
        if state.http_client is None:
            state.http_client = httpx.AsyncClient(timeout=10.0)
            
        if state.pathfinder is None:
             raise HTTPException(status_code=503, detail="Routing service not initialized")

        # 1. Build congestion data from cache
        congestion_data = {
            "cells": [
                {"cell_id": cid, "congestion_level": level}
                for cid, level in state.congestion_cache.items()
            ]
        }

        # 2. Find nearest node to start (LOCAL LOOKUP)
        start_node_id = state.pathfinder.find_nearest_node(
            request.start.x, 
            request.start.y, 
            request.start.level
        )
        
        if not start_node_id:
            logger.error(f"[ROUTE ERROR-A] No nearby node for start ({request.start.x}, {request.start.y}, level={request.start.level})")
            raise HTTPException(status_code=404, detail="No nearby node found for starting position")
        
        # 3. Get destination node based on type
        end_node_id = None
        wait_time = None
        
        if request.destination_type in ["node", "node_id"]:
            end_node_id = request.destination_id
        
        elif request.destination_type == "poi":
            # 1. Try lookup from RAM cache
            poi = next((p for p in state.poi_cache if p['id'] == request.destination_id), None)
            
            # 2. Fallback: Try with alternative prefix (OSM- vs POI-) if it looks like an OSM ID
            if not poi:
                alt_id = None
                if request.destination_id.startswith("POI-"):
                    alt_id = request.destination_id.replace("POI-", "OSM-")
                elif request.destination_id.startswith("OSM-"):
                    alt_id = request.destination_id.replace("OSM-", "POI-")
                
                if alt_id:
                    poi = next((p for p in state.poi_cache if p['id'] == alt_id), None)
                    if poi:
                        logger.info(f"[ROUTE] Found POI via alternative ID: {alt_id}")

            # 3. Fallback: Fetch directly from Map Service if still missing
            if not poi:
                logger.info(f"[ROUTE] POI {request.destination_id} not in cache, fetching from Map Service...")
                try:
                    headers = _get_api_headers()
                    resp = await state.http_client.get(f"{MAP_SERVICE_URL}/pois/{request.destination_id}", headers=headers, timeout=5.0)
                    if resp.status_code == 200:
                        poi = resp.json()
                        logger.info(f"[ROUTE] Fetched POI {request.destination_id} directly from Map Service")
                except Exception as e:
                    logger.warning(f"[ROUTE] Direct POI fetch failed: {e}")

            if not poi:
                logger.error(f"[ROUTE ERROR-B] POI not found: {request.destination_id}")
                raise HTTPException(status_code=404, detail=f"POI {request.destination_id} not found")
            
            if poi.get('nearest_node_id') and poi['nearest_node_id'] in state.pathfinder.nodes:
                end_node_id = poi['nearest_node_id']
            else:
                end_node_id = state.pathfinder.find_nearest_node(
                    poi['x'], poi['y'], poi.get('level', 0)
                )
            
            wait_time = state.waittime_cache.get(request.destination_id, 0)
        
        elif request.destination_type in ["seat", "gate"]:
            endpoint = f"/{request.destination_type}s/{request.destination_id}"
            headers = _get_api_headers()
            response = await state.http_client.get(f"{MAP_SERVICE_URL}{endpoint}", headers=headers)
            if response.status_code != 200:
                logger.error(f"[ROUTE ERROR-C] {request.destination_type.title()} not found: {request.destination_id} (Map Service returned {response.status_code})")
                raise HTTPException(status_code=404, detail=f"{request.destination_type.title()} not found")
            dest = response.json()
            
            end_node_id = state.pathfinder.find_nearest_node(
                dest['x'], dest['y'], dest['level']
            )
        
        elif request.destination_type == "nearest_category":
            category = request.destination_id
            category_pois = [
                p for p in state.poi_cache 
                if p.get('type', '').lower() == category.lower() or 
                   p.get('category', '').lower() == category.lower() or
                   p['id'].lower().startswith(category.lower() + '-')
            ]
            
            if not category_pois:
                raise HTTPException(status_code=404, detail=f"No POIs found for category {category}")
            
            best_poi = None
            best_cost = float('inf')
            best_poi_node = None
            
            for poi in category_pois:
                poi_node = state.pathfinder.find_nearest_node(poi['x'], poi['y'], poi['level'])
                if not poi_node:
                    continue
                
                route, travel_cost = state.pathfinder.find_path(
                    start_node_id,
                    poi_node,
                    congestion_data,
                    avoid_stairs=request.avoid_stairs,
                    waittime_data=state.waittime_cache,
                    blocked_nodes=state.active_closures
                )
                
                if not route:
                    continue
                
                poi_wait = state.waittime_cache.get(poi['id'], 0) * 60
                total_cost = travel_cost + poi_wait
                
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_poi = poi
                    best_poi_node = poi_node
            
            if not best_poi:
                raise HTTPException(status_code=404, detail=f"No accessible POI found in category {category}")
            
            end_node_id = best_poi_node
            wait_time = state.waittime_cache.get(best_poi['id'], 0)
            request.destination_id = best_poi['id']
        
        if not end_node_id:
            logger.error(f"[ROUTE ERROR-F] Destination node not found (destination_type={request.destination_type}, destination_id={request.destination_id})")
            raise HTTPException(status_code=404, detail="Destination node not found")
        
        # 4. Find path using A*
        route, path_cost = state.pathfinder.find_path(
            start_node_id,
            end_node_id,
            congestion_data,
            avoid_stairs=request.avoid_stairs,
            waittime_data=state.waittime_cache,
            blocked_nodes=state.active_closures
        )
        
        if not route:
            logger.error(f"[ROUTE ERROR-E] No path found between {start_node_id} and {end_node_id}")
            raise HTTPException(status_code=404, detail="No path found between the selected points")
        
        path_ids = route
        total_cost = path_cost
        
        # 5. Build response
        path_nodes = []
        cumulative_distance = 0
        waypoints = []  # Track important decision points
        
        for i, node_id in enumerate(path_ids):
            node = state.pathfinder.nodes[node_id]
            
            if i > 0:
                prev_node = state.pathfinder.nodes[path_ids[i-1]]
                cumulative_distance += state.pathfinder.calculate_distance(
                    prev_node['x'], prev_node['y'],
                    node['x'], node['y']
                )
            
            is_waypoint = False
            if i > 0 and i < len(path_ids) - 1:
                neighbors = state.pathfinder.graph.get(node_id, [])
                if len(neighbors) > 2 or node['level'] != state.pathfinder.nodes[path_ids[i-1]]['level']:
                    is_waypoint = True
                    waypoints.append(node_id)
            
            path_nodes.append(PathNode(
                node_id=node_id,
                x=node['x'],
                y=node['y'],
                level=node['level'],
                distance_from_start=cumulative_distance,
                estimated_time=cumulative_distance / WALKING_SPEED,
                is_waypoint=is_waypoint
            ))
        
        # Calculate average congestion
        congestion_map = {c.get('cell_id', c.get('id')): c['congestion_level'] for c in congestion_data.get('cells', [])}
        avg_congestion = sum(congestion_map.get(nid, 0.0) for nid in path_ids) / len(path_ids) if path_ids else 1.0
        
        session_id = None
        mqtt_topic = None
        if state.session_manager:
            import uuid
            effective_ticket_id = request.ticket_id or f"anon-{uuid.uuid4().hex[:8]}"
            session = state.session_manager.create_session(
                ticket_id=effective_ticket_id,
                start_node=start_node_id,
                end_node=end_node_id,
                destination_type=request.destination_type,
                destination_id=request.destination_id,
                route=path_ids,
                total_cost=(path_cost / WALKING_SPEED),  # Store travel time in seconds
                avoid_stairs=request.avoid_stairs
            )
            session_id = session.session_id
            mqtt_topic = f"stadium/services/routing/{session_id}"
        
        return RouteResponse(
            path=path_nodes,
            total_distance=cumulative_distance,
            estimated_time=cumulative_distance / WALKING_SPEED,
            congestion_level=avg_congestion,
            wait_time=wait_time,
            warnings=["High congestion"] if avg_congestion > 0.7 else [],
            session_id=session_id,
            mqtt_topic=mqtt_topic,
            waypoints=waypoints
        )
    
    except HTTPException:
        raise
    except Exception:
        logger.exception("[API] Route calculation error")
        raise HTTPException(status_code=500, detail="Internal server error during route calculation")

@app.get("/health")
async def health_check():
    """Service health check endpoint"""
    return {
        "status": "healthy",
        "service": "routing",
        "map_service": MAP_SERVICE_URL,
        "congestion_service": CONGESTION_SERVICE_URL
    }