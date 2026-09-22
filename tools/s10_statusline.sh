#!/bin/sh
# This repo's statusline: whatever the user's own statusline prints (Orca's
# here), then a second line for the S10 run in progress, if there is one.
input=$(cat)
orca="$HOME/.orca/agent-hooks/claude-statusline.sh"
if [ -x "$orca" ]; then printf '%s' "$input" | /bin/sh "$orca"; echo; fi
exec python3 "$(dirname "$0")/s10_status.py"
