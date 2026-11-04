#!/bin/sh
# Remove the launchd agent, the command link and RouteC.app. Leaves logs, journal and the lease file.
set -u
LABEL=com.ekorre.routec
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
for b in /opt/homebrew/bin/routec "$HOME/.local/bin/routec"; do [ -L "$b" ] && rm -f "$b"; done
rm -rf "$HOME/Applications/RouteC.app"
echo "Route C removed. Logs: ~/Library/Logs/RouteC  State: ~/Library/Application Support/RouteC"
