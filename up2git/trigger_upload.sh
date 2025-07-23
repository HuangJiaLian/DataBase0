#!/bin/bash

# Script to trigger clipboard upload
# This script should be assigned to Alt+Shift+U in system keyboard shortcuts

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if the main application is running
if pgrep -f "python.*main.py" > /dev/null; then
    # Send a signal to the running application to upload from clipboard
    # We'll use a temporary file as a simple IPC mechanism
    touch "$SCRIPT_DIR/.upload_trigger"
else
    # Start the application if not running
    "$SCRIPT_DIR/run.sh" &
    # Wait a moment for it to start, then trigger upload
    sleep 3
    touch "$SCRIPT_DIR/.upload_trigger"
fi
