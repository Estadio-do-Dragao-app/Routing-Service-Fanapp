import time
import logging
from typing import Dict, Optional, List, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from pathFinding import PathFinder

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """Represents a position update from the client"""
    node_id: Optional[str]
    timestamp: float
    x: Optional[float] = None
    y: Optional[float] = None
    level: Optional[int] = None


@dataclass
class RouteSession:
    """Tracks an active routing session for a user"""
    session_id: str
    ticket_id: str
    start_node: str
    end_node: str
    destination_type: str
    destination_id: str
    current_route: List[str]  # List of node IDs
    total_cost: float
    start_time: float
    last_heartbeat: float
    last_checkpoint: Optional[Checkpoint] = None
    is_active: bool = True
    avoid_stairs: bool = False
    
    # Session configuration
    SESSION_TIMEOUT: float = 300  # 5 minutes
    HEARTBEAT_TIMEOUT: float = 120  # 2 minutes
    
    def update_heartbeat(self):
        """Update last heartbeat timestamp"""
        self.last_heartbeat = time.time()
        logger.debug(f"Session {self.session_id}: Heartbeat updated")
    
    def update_checkpoint(self, checkpoint: Checkpoint):
        """Update position from waypoint"""
        self.last_checkpoint = checkpoint
        logger.info(f"Session {self.session_id}: Checkpoint updated at node {checkpoint.node_id}")
    
    def is_expired(self) -> bool:
        """Check if session has expired"""
        return (time.time() - self.start_time) > self.SESSION_TIMEOUT
    
    def is_stale(self) -> bool:
        """Check if client hasn't sent heartbeat recently"""
        return (time.time() - self.last_heartbeat) > self.HEARTBEAT_TIMEOUT
    
    def estimate_current_position(self, pathfinder: PathFinder) -> Tuple[str, float]:
        """
        Estimate current position along route.
        Returns (estimated_node_id, confidence)
        """
        if self.last_checkpoint:
            time_since_checkpoint = time.time() - self.last_checkpoint.timestamp
        else:
            time_since_checkpoint = time.time() - self.start_time
        
        # Calculate confidence based on time since last update
        if time_since_checkpoint < 30:
            confidence = 0.9
        elif time_since_checkpoint < 60:
            confidence = 0.7
        elif time_since_checkpoint < 90:
            confidence = 0.5
        else:
            confidence = 0.3
        
        # Estimate distance traveled (assuming 1.4 m/s walking speed)
        distance_traveled = time_since_checkpoint * 1.4
        
        # Find position along route
        if self.last_checkpoint and self.last_checkpoint.node_id:
            # Start from last known checkpoint
            try:
                start_idx = self.current_route.index(self.last_checkpoint.node_id)
            except ValueError:
                start_idx = 0
        else:
            start_idx = 0
        
        # Walk along route until we've covered distance_traveled
        cumulative_dist = 0
        estimated_node = self.current_route[start_idx]
        
        for i in range(start_idx, len(self.current_route) - 1):
            node1 = pathfinder.nodes.get(self.current_route[i])
            node2 = pathfinder.nodes.get(self.current_route[i + 1])
            
            if not node1 or not node2:
                continue
            
            segment_dist = pathfinder.calculate_distance(
                node1['x'], node1['y'],
                node2['x'], node2['y']
            )
            
            if cumulative_dist + segment_dist >= distance_traveled:
                estimated_node = self.current_route[i + 1]
                break
            
            cumulative_dist += segment_dist
        
        return estimated_node, confidence


class RouteSessionManager:
    """Manages active routing sessions"""
    
    def __init__(self, pathfinder: PathFinder):
        self.pathfinder = pathfinder
        self.sessions: Dict[str, RouteSession] = {}
        self.ticket_sessions: Dict[str, str] = {}  # ticket_id -> session_id mapping
    
    def create_session(
        self,
        ticket_id: str,
        start_node: str,
        end_node: str,
        destination_type: str,
        destination_id: str,
        route: List[str],
        total_cost: float,
        avoid_stairs: bool = False
    ) -> RouteSession:
        """Create a new routing session"""
        session_id = f"route-{ticket_id}-{int(time.time())}"
        
        # Cancel existing session for this ticket if any
        if ticket_id in self.ticket_sessions:
            old_session_id = self.ticket_sessions[ticket_id]
            if old_session_id in self.sessions:
                self.sessions[old_session_id].is_active = False
                logger.info(f"Cancelled previous session {old_session_id} for ticket {ticket_id}")
        
        session = RouteSession(
            session_id=session_id,
            ticket_id=ticket_id,
            start_node=start_node,
            end_node=end_node,
            destination_type=destination_type,
            destination_id=destination_id,
            current_route=route,
            total_cost=total_cost,
            start_time=time.time(),
            last_heartbeat=time.time(),
            avoid_stairs=avoid_stairs
        )
        
        self.sessions[session_id] = session
        self.ticket_sessions[ticket_id] = session_id
        
        logger.info(f"Created new session {session_id} for ticket {ticket_id}")
        return session
    
    def get_session(self, session_id: str) -> Optional[RouteSession]:
        """Get session by ID"""
        return self.sessions.get(session_id)
    
    def get_ticket_session(self, ticket_id: str) -> Optional[RouteSession]:
        """Get active session for a ticket"""
        session_id = self.ticket_sessions.get(ticket_id)
        if session_id:
            return self.sessions.get(session_id)
        return None
    
    def handle_heartbeat(self, ticket_id: str, payload: dict):
        """Process heartbeat from client"""
        session = self.get_ticket_session(ticket_id)
        if session and session.is_active:
            session.update_heartbeat()
    
    def handle_waypoint(self, ticket_id: str, payload: dict):
        """Process waypoint update from client"""
        session = self.get_ticket_session(ticket_id)
        if session and session.is_active:
            checkpoint = Checkpoint(
                node_id=payload.get('node_id'),
                timestamp=payload.get('timestamp', time.time()),
                x=payload.get('x'),
                y=payload.get('y'),
                level=payload.get('level')
            )
            session.update_checkpoint(checkpoint)
    
    def handle_cancellation(self, ticket_id: str, payload: dict):
        """Handle route cancellation from client"""
        session = self.get_ticket_session(ticket_id)
        if session:
            session.is_active = False
            logger.info(f"Session {session.session_id} cancelled by user")
    
    def cleanup_expired_sessions(self) -> List[str]:
        """Remove expired and stale sessions. Returns list of expired session IDs."""
        expired = []
        
        for session_id, session in list(self.sessions.items()):
            if session.is_expired() or session.is_stale():
                logger.info(f"Removing expired/stale session: {session_id}")
                expired.append(session_id)
                
                # Remove from ticket mapping
                if session.ticket_id in self.ticket_sessions:
                    if self.ticket_sessions[session.ticket_id] == session_id:
                        del self.ticket_sessions[session.ticket_id]
                
                # Remove session
                del self.sessions[session_id]
        
        return expired
    
    def get_active_sessions(self) -> List[RouteSession]:
        """Get all active sessions"""
        return [s for s in self.sessions.values() if s.is_active and not s.is_stale()]
    
    def should_reroute(
        self,
        session: RouteSession,
        new_route: List[str],
        new_cost: float,
        reason: str
    ) -> Optional[dict]:
        """
        Determine if rerouting suggestion should be sent.
        Returns suggestion dict if yes, None otherwise.
        """
        # Don't suggest if session is stale
        if session.is_stale():
            return None
        
        # Estimate current position and confidence
        estimated_node, confidence = session.estimate_current_position(self.pathfinder)
        
        # Calculate improvement
        cost_improvement = (session.total_cost - new_cost) / session.total_cost
        
        # Adjust threshold based on confidence
        if confidence > 0.7:
            threshold = 0.25  # 25% improvement needed
        elif confidence > 0.4:
            threshold = 0.40  # 40% improvement needed
        else:
            threshold = 0.50  # 50% improvement needed (low confidence)
        
        # Check if improvement is significant enough
        if cost_improvement > threshold:
            time_saved = (session.total_cost - new_cost) / 1.4  # Convert to seconds
            
            return {
                "type": "reroute_suggestion",
                "session_id": session.session_id,
                "reason": reason,
                "confidence": round(confidence, 2),
                "current_estimate_node": estimated_node,
                "new_route": new_route,
                "improvement": {
                    "cost_reduction": round(cost_improvement * 100, 1),
                    "time_saved_seconds": round(time_saved, 0),
                    "time_saved_display": f"{int(time_saved // 60)}min {int(time_saved % 60)}s"
                }
            }
        
        return None
