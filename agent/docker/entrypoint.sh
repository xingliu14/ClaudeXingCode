#!/bin/bash
# Configure gh as the git credential helper so `git push` works over HTTPS
# when GH_TOKEN is available. This must run at container start (not build time)
# because GH_TOKEN is injected at runtime by the dispatcher.
if [ -n "$GH_TOKEN" ]; then
    gh auth setup-git
fi
exec "$@"
