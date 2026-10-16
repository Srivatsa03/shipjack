#!/bin/sh
# Start one of the two Python services in this image: SHIPJACK_SERVICE=store (default) or assistant.
# One plain path for the platform to run, so no host has to parse quotes in a start command.
set -e
case "${SHIPJACK_SERVICE:-store}" in
  store) exec shipjack-serve --host 0.0.0.0 --port "${PORT:-8000}" ;;
  assistant) exec shipjack-assistant --host 0.0.0.0 --port "${PORT:-8200}" ;;
  *) echo "start.sh: SHIPJACK_SERVICE must be store or assistant, not '${SHIPJACK_SERVICE}'" >&2; exit 2 ;;
esac
