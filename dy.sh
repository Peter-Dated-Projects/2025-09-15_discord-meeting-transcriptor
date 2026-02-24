#!/bin/bash

# Service management script for Discord Meeting Transcriptor
# Usage: ./dy.sh [up|down|run|restart|destroy|status|logs]

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# Load environment variables from .env.local
ENV_FILE="$PROJECT_ROOT/.env.local"

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: .env.local file not found at $ENV_FILE"
    exit 1
fi

# Source the environment file
set -a
source "$ENV_FILE"
set +a

# Docker Compose file
DOCKER_COMPOSE_FILE="$PROJECT_ROOT/docker-compose.local.yml"

# PID file locations
OLLAMA_PID_FILE="$PROJECT_ROOT/.ollama.pid"
WHISPER_PID_FILE="$PROJECT_ROOT/.whisper.pid"
CHROMADB_ADMIN_PID_FILE="$PROJECT_ROOT/.chromadb_admin.pid"

# Log file locations
LOGS_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOGS_DIR"
OLLAMA_LOG="$LOGS_DIR/ollama.log"
WHISPER_LOG="$LOGS_DIR/whisper.log"
CHROMADB_ADMIN_LOG="$LOGS_DIR/chromadb_admin.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to check if Docker is available
check_docker() {
    if ! command -v docker &> /dev/null; then
        echo -e "${RED}Error: Docker is not installed or not in PATH${NC}"
        return 1
    fi
    
    if ! docker info &> /dev/null; then
        echo -e "${RED}Error: Docker daemon is not running${NC}"
        return 1
    fi
    
    return 0
}

# Function to check if a process is running
is_running() {
    local pid_file=$1
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        else
            rm -f "$pid_file"
            return 1
        fi
    fi
    return 1
}

# Function to start Docker Compose services
start_docker_services() {
    echo -e "${BLUE}Starting Docker Compose services...${NC}"
    
    if [ ! -f "$DOCKER_COMPOSE_FILE" ]; then
        echo -e "${YELLOW}Warning: docker-compose.local.yml not found${NC}"
        return 1
    fi
    
    if ! check_docker; then
        return 1
    fi
    
    docker compose -f "$DOCKER_COMPOSE_FILE" up -d
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Docker services started successfully${NC}"
        return 0
    else
        echo -e "${RED}✗ Failed to start Docker services${NC}"
        return 1
    fi
}

# Function to stop Docker Compose services
stop_docker_services() {
    echo -e "${BLUE}Stopping Docker Compose services...${NC}"
    
    if [ ! -f "$DOCKER_COMPOSE_FILE" ]; then
        echo -e "${YELLOW}Warning: docker-compose.local.yml not found${NC}"
        return 1
    fi
    
    if ! check_docker; then
        return 1
    fi
    
    docker compose -f "$DOCKER_COMPOSE_FILE" stop
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Docker services stopped${NC}"
        return 0
    else
        echo -e "${RED}✗ Failed to stop Docker services${NC}"
        return 1
    fi
}

# Function to destroy Docker Compose services (remove containers)
destroy_docker_services() {
    echo -e "${BLUE}Destroying Docker Compose services...${NC}"
    
    if [ ! -f "$DOCKER_COMPOSE_FILE" ]; then
        echo -e "${YELLOW}Warning: docker-compose.local.yml not found${NC}"
        return 1
    fi
    
    if ! check_docker; then
        return 1
    fi
    
    docker compose -f "$DOCKER_COMPOSE_FILE" down
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Docker services destroyed${NC}"
        return 0
    else
        echo -e "${RED}✗ Failed to destroy Docker services${NC}"
        return 1
    fi
}

# Function to get Docker service status
get_docker_status() {
    if ! check_docker &> /dev/null; then
        return 1
    fi
    
    if [ ! -f "$DOCKER_COMPOSE_FILE" ]; then
        return 1
    fi
    
    docker compose -f "$DOCKER_COMPOSE_FILE" ps --format json 2>/dev/null | grep -q "running"
    return $?
}

# Function to start Ollama server
start_ollama() {
    echo -e "${BLUE}Starting Ollama server...${NC}"
    
    if is_running "$OLLAMA_PID_FILE"; then
        echo -e "${YELLOW}Ollama is already running (PID: $(cat $OLLAMA_PID_FILE))${NC}"
        return 0
    fi
    
    # Check if ollama command exists
    if ! command -v ${OLLAMA_COMMAND_PATH:-ollama} &> /dev/null; then
        echo -e "${RED}Error: Ollama command not found. Please install Ollama first.${NC}"
        return 1
    fi
    
    # Start Ollama in background
    export OLLAMA_HOST="${OLLAMA_HOST:-localhost}"
    export OLLAMA_PORT="${OLLAMA_PORT:-11434}"
    
    nohup ${OLLAMA_COMMAND_PATH:-ollama} serve > "$OLLAMA_LOG" 2>&1 &
    local pid=$!
    echo $pid > "$OLLAMA_PID_FILE"
    
    sleep 2
    
    if is_running "$OLLAMA_PID_FILE"; then
        echo -e "${GREEN}✓ Ollama server started successfully (PID: $pid)${NC}"
        echo -e "  Host: ${OLLAMA_HOST}:${OLLAMA_PORT}"
        echo -e "  Logs: $OLLAMA_LOG"
        return 0
    else
        echo -e "${RED}✗ Failed to start Ollama server${NC}"
        return 1
    fi
}

# Function to start Whisper Flask microservice
start_whisper() {
    echo -e "${BLUE}Starting Whisper Flask microservice...${NC}"
    
    if is_running "$WHISPER_PID_FILE"; then
        echo -e "${YELLOW}Whisper service is already running (PID: $(cat $WHISPER_PID_FILE))${NC}"
        return 0
    fi
    
    # Check if run script exists
    RUN_SCRIPT="$PROJECT_ROOT/scripts/run_whisper_service.sh"
    if [ ! -f "$RUN_SCRIPT" ]; then
        echo -e "${RED}✗ Whisper service run script not found at $RUN_SCRIPT${NC}"
        return 1
    fi
    
    # Start Flask microservice in background using the run script
    nohup "$RUN_SCRIPT" --background > "$WHISPER_LOG" 2>&1 &
    local pid=$!
    echo $pid > "$WHISPER_PID_FILE"
    
    # Wait a moment for service to start
    sleep 3
    
    if is_running "$WHISPER_PID_FILE"; then
        # Get Flask configuration from environment
        FLASK_HOST="${FLASK_HOST:-0.0.0.0}"
        FLASK_PORT="${FLASK_PORT:-5000}"
        
        echo -e "${GREEN}✓ Whisper Flask microservice started successfully (PID: $pid)${NC}"
        echo -e "  API: http://${FLASK_HOST}:${FLASK_PORT}"
        echo -e "  Health: http://${FLASK_HOST}:${FLASK_PORT}/health"
        echo -e "  Inference: http://${FLASK_HOST}:${FLASK_PORT}/inference"
        echo -e "  Logs: $WHISPER_LOG"
        return 0
    else
        echo -e "${RED}✗ Failed to start Whisper Flask microservice${NC}"
        echo -e "  Check logs at: $WHISPER_LOG"
        return 1
    fi
}

# Function to start ChromaDB Admin Dashboard
start_chromadb_admin() {
    echo -e "${BLUE}Starting ChromaDB Admin Dashboard...${NC}"
    
    if is_running "$CHROMADB_ADMIN_PID_FILE"; then
        echo -e "${YELLOW}ChromaDB Admin Dashboard is already running (PID: $(cat $CHROMADB_ADMIN_PID_FILE))${NC}"
        return 0
    fi
    
    # Check if admin page script exists
    ADMIN_SCRIPT="$PROJECT_ROOT/scripts/chromadb/admin_page.py"
    if [ ! -f "$ADMIN_SCRIPT" ]; then
        echo -e "${RED}✗ ChromaDB Admin page script not found at $ADMIN_SCRIPT${NC}"
        return 1
    fi
    
    # Start admin dashboard in background using `uv run`
    CHROMADB_HOST="${CHROMADB_HOST:-0.0.0.0}"
    CHROMADB_PORT="${CHROMADB_PORT:-8000}"

    # Use `uv run` to launch the ASGI admin app. Expects an importable module
    # `admin_page` under scripts/chromadb that exposes the ASGI app. If the
    # app symbol name differs, set `CHROMADB_ADMIN_APP` in your environment to
    # the correct module:app (e.g. "admin_page:my_app").
    CHROMADB_ADMIN_APP="${CHROMADB_ADMIN_APP:-scripts/chromadb/admin_page.py}"

    nohup uv run "$CHROMADB_ADMIN_APP" --app-dir "$PROJECT_ROOT/scripts/chromadb" --host "${CHROMADB_HOST}" --port "${CHROMADB_PORT}" > "$CHROMADB_ADMIN_LOG" 2>&1 &
    local pid=$!
    echo $pid > "$CHROMADB_ADMIN_PID_FILE"
    
    # Wait a moment for service to start
    sleep 2
    
    if is_running "$CHROMADB_ADMIN_PID_FILE"; then
        CHROMADB_HOST="${CHROMADB_HOST:-localhost}"
        CHROMADB_PORT="${CHROMADB_PORT:-8000}"
        
        echo -e "${GREEN}✓ ChromaDB Admin Dashboard started successfully (PID: $pid)${NC}"
        echo -e "  Dashboard: http://localhost:3002"
        echo -e "  ChromaDB: ${CHROMADB_HOST}:${CHROMADB_PORT}"
        echo -e "  Logs: $CHROMADB_ADMIN_LOG"
        return 0
    else
        echo -e "${RED}✗ Failed to start ChromaDB Admin Dashboard${NC}"
        echo -e "  Check logs at: $CHROMADB_ADMIN_LOG"
        return 1
    fi
}

# Function to stop a service
stop_service() {
    local service_name=$1
    local pid_file=$2
    
    if ! is_running "$pid_file"; then
        echo -e "${YELLOW}$service_name is not running${NC}"
        return 0
    fi
    
    local pid=$(cat "$pid_file")
    echo -e "${BLUE}Stopping $service_name (PID: $pid)...${NC}"
    
    kill "$pid" 2>/dev/null
    
    # Wait for process to stop
    local count=0
    while ps -p "$pid" > /dev/null 2>&1 && [ $count -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done
    
    # Force kill if still running
    if ps -p "$pid" > /dev/null 2>&1; then
        echo -e "${YELLOW}Force stopping $service_name...${NC}"
        kill -9 "$pid" 2>/dev/null
    fi
    
    rm -f "$pid_file"
    echo -e "${GREEN}✓ $service_name stopped${NC}"
}

# Function to show status
show_status() {
    echo -e "${BLUE}=== Service Status ===${NC}"
    
    echo -e "\n${BLUE}Docker Services:${NC}"
    if check_docker &> /dev/null && [ -f "$DOCKER_COMPOSE_FILE" ]; then
        docker compose -f "$DOCKER_COMPOSE_FILE" ps
    else
        echo -e "${YELLOW}Docker not available or compose file missing${NC}"
    fi
    
    echo -e "\n${BLUE}Native Services:${NC}"
    echo -n "Ollama:         "
    if is_running "$OLLAMA_PID_FILE"; then
        echo -e "${GREEN}Running${NC} (PID: $(cat $OLLAMA_PID_FILE))"
    else
        echo -e "${RED}Stopped${NC}"
    fi
    
    echo -n "Whisper Flask:  "
    if is_running "$WHISPER_PID_FILE"; then
        echo -e "${GREEN}Running${NC} (PID: $(cat $WHISPER_PID_FILE))"
        FLASK_PORT="${FLASK_PORT:-5000}"
        echo -e "                API: http://localhost:${FLASK_PORT}"
    else
        echo -e "${RED}Stopped${NC}"
    fi
    
    echo -n "ChromaDB Admin: "
    if is_running "$CHROMADB_ADMIN_PID_FILE"; then
        echo -e "${GREEN}Running${NC} (PID: $(cat $CHROMADB_ADMIN_PID_FILE))"
        echo -e "                Dashboard: http://localhost:3002"
    else
        echo -e "${RED}Stopped${NC}"
    fi
}

# Main command handling
case "${1:-}" in
    up)
        echo -e "${BLUE}=== Starting All Services ===${NC}"
        start_docker_services
        echo ""
        start_ollama
        start_whisper
        start_chromadb_admin
        echo ""
        show_status
        ;;
    run)
        echo -e "${BLUE}=== Starting All Services ===${NC}"
        start_docker_services
        echo ""
        start_ollama
        start_whisper
        start_chromadb_admin
        echo ""
        show_status
        echo ""
        echo -e "${BLUE}=== Running Application ===${NC}"
        uv run main.py
        ;;
    down)
        echo -e "${BLUE}=== Stopping All Services ===${NC}"
        stop_service "Ollama" "$OLLAMA_PID_FILE"
        stop_service "Whisper Flask" "$WHISPER_PID_FILE"
        stop_service "ChromaDB Admin Dashboard" "$CHROMADB_ADMIN_PID_FILE"
        echo ""
        stop_docker_services
        echo ""
        show_status
        ;;
    restart)
        echo -e "${BLUE}=== Restarting All Services ===${NC}"
        stop_service "Ollama" "$OLLAMA_PID_FILE"
        stop_service "Whisper Flask" "$WHISPER_PID_FILE"
        stop_service "ChromaDB Admin Dashboard" "$CHROMADB_ADMIN_PID_FILE"
        stop_docker_services
        echo ""
        sleep 2
        start_docker_services
        echo ""
        start_ollama
        start_whisper
        start_chromadb_admin
        echo ""
        show_status
        ;;
    destroy)
        echo -e "${BLUE}=== Destroying All Services ===${NC}"
        stop_service "Ollama" "$OLLAMA_PID_FILE"
        stop_service "Whisper Flask" "$WHISPER_PID_FILE"
        stop_service "ChromaDB Admin Dashboard" "$CHROMADB_ADMIN_PID_FILE"
        echo ""
        destroy_docker_services
        echo ""
        show_status
        ;;
    status)
        show_status
        ;;
    logs)
        echo -e "${BLUE}=== Service Logs ===${NC}"
        echo -e "\n${BLUE}Available log files in $LOGS_DIR:${NC}"
        if [ -d "$LOGS_DIR" ]; then
            ls -lh "$LOGS_DIR"
        else
            echo -e "${YELLOW}Logs directory not found${NC}"
        fi
        echo -e "\n${BLUE}Docker service logs:${NC}"
        echo -e "Use: ${GREEN}docker compose -f $DOCKER_COMPOSE_FILE logs [service_name]${NC}"
        echo -e "Services: mysql, chromadb"
        ;;
    *)
        echo "Usage: $0 {up|down|run|restart|destroy|status|logs}"
        echo ""
        echo "Commands:"
        echo "  up      - Start all services (Docker, Ollama, Whisper Flask & ChromaDB Admin)"
        echo "  run     - Start all services and run the application (uv run main.py)"
        echo "  down    - Stop all services (keeps containers)"
        echo "  restart - Restart all services"
        echo "  destroy - Stop and remove all Docker containers"
        echo "  status  - Show service status"
        echo "  logs    - Show log file locations"
        echo ""
        echo "Services:"
        echo "  Docker:         MySQL, ChromaDB (via docker-compose.local.yml)"
        echo "  Ollama:         LLM service for chat/RAG"
        echo "  Whisper Flask:  Transcription microservice API"
        echo "  ChromaDB Admin: Web dashboard for ChromaDB (http://localhost:3002)"
        exit 1
        ;;
esac
