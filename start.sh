#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 -c "import PIL" 2>/dev/null || python3 -m pip install --quiet pillow
exec python3 launcher.py --port 8790
