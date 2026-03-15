# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# --no-cache-dir reduces image size
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose port 8002 to the outside world
EXPOSE 8002

# Define environment variables (optional defaults, can be overridden)
ENV MAP_SERVICE_URL="http://host.docker.internal:8001"
ENV CONGESTION_SERVICE_URL="http://host.docker.internal:8003"
ENV CLIENT_BROKER="localhost"
ENV CLIENT_PORT="1884"
ENV STADIUM_BROKER="localhost"
ENV STADIUM_PORT="1883"

# Run the app
CMD ["python", "main.py"]
