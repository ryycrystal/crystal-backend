#!/bin/bash
set -euo pipefail
python -c "import os, urllib.request; urllib.request.urlretrieve(os.environ['RUNNER_URL'], '/tmp/runner.sh')"
exec bash /tmp/runner.sh
