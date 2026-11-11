#!/bin/zsh
# Launchd entrypoint for the complete SmartHome stack on macOS.
# Arguments: project root

set -u

# launchd does not load the interactive shell profile. Include the usual
# Homebrew and Docker locations explicitly so reboot startup matches Terminal.
export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Ollama.app/Contents/Resources:/Applications/OrbStack.app/Contents/MacOS:/usr/bin:/bin:/usr/sbin:/sbin"
# Keep Ollama's model store on the large Data volume.  Ollama expects this
# directory itself to contain blobs/ and manifests/.
export OLLAMA_MODELS="/Volumes/Data/AIModels"

PROJECT_ROOT="${1:-}"
print -u2 "SmartHome launch: root=$PROJECT_ROOT"
if [[ -z "$PROJECT_ROOT" || ! -f "$PROJECT_ROOT/server.py" ]]; then
  print -u2 "SmartHome: project root is missing or invalid: $PROJECT_ROOT"
  exit 78
fi

cd "$PROJECT_ROOT" || exit 78
print -u2 "SmartHome launch: root valid"

# launchd opens its own log files before the external Data volume is always
# available. Once the project root is reachable, keep the real application
# logs in the project as requested.
mkdir -p "$PROJECT_ROOT/Logs"
exec >> "$PROJECT_ROOT/Logs/server.log" 2>> "$PROJECT_ROOT/Logs/server.err.log"

# OrbStack is a GUI application, so launchd cannot start it as a Docker daemon.
# Starting it here is harmless when it is already running.
if (( $+commands[open] )); then
  open -gj -a OrbStack >/dev/null 2>&1 || true
fi

# Wait for OrbStack's Docker socket after login/reboot. Exiting makes launchd
# retry the whole service if OrbStack is unavailable for an extended period.
docker_ready=0
for _ in {1..60}; do
  if docker info >/dev/null 2>&1; then
    docker_ready=1
    break
  fi
  sleep 5
done

if (( ! docker_ready )); then
  print -u2 "SmartHome: Docker/OrbStack did not become ready"
  exit 75
fi
print -u2 "SmartHome launch: Docker ready"

COMPOSE_FILE="$PROJECT_ROOT/data/frigate/docker-compose.mac.yml"
if [[ ! -f "$COMPOSE_FILE" ]]; then
  COMPOSE_FILE="$PROJECT_ROOT/deploy/macos/docker-compose.mac.yml"
fi
if [[ -f "$COMPOSE_FILE" ]]; then
  docker compose -f "$COMPOSE_FILE" up -d || exit $?
fi

PYTHON="$PROJECT_ROOT/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  print -u2 "SmartHome: python3 was not found"
  exit 78
fi
print -u2 "SmartHome launch: Python=$PYTHON"

exec "$PYTHON" -X utf8 "$PROJECT_ROOT/server.py"
