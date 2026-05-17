# Routing Service Constants

# Walking speed in meters per second (approx. 5 km/h)
WALKING_SPEED = 1.4

# Session configuration (seconds)
SESSION_TIMEOUT = 300       # 5 minutes
HEARTBEAT_TIMEOUT = 120    # 2 minutes
CLEANUP_INTERVAL = 60      # 1 minute

# Rerouting thresholds (improvement percentage required)
REROUTE_THRESHOLD_HIGH_CONF = 0.25
REROUTE_THRESHOLD_MED_CONF = 0.40
REROUTE_THRESHOLD_LOW_CONF = 0.50

# Wait time penalty conversion
# 1 minute wait = distance equivalent in meters
WAIT_TIME_DISTANCE_PENALTY = WALKING_SPEED * 60  # 84 meters per minute
