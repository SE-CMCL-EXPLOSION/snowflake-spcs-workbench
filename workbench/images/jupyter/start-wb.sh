#!/bin/bash
# Token proxy on 8080 (as root), JupyterLab on 8888 at the root of the session's own endpoint
mkdir -p /home/jovyan/work
[ -d /home/jovyan/scratch ] && chown 1000:100 /home/jovyan/scratch
python3 /opt/wb/proxy.py &
exec /usr/local/bin/start.sh jupyter lab \
  --ip=127.0.0.1 --port=8888 --no-browser \
  --ServerApp.base_url=/ \
  --ServerApp.root_dir=/home/jovyan \
  --IdentityProvider.token='' --ServerApp.password='' \
  --ServerApp.allow_origin='*' --ServerApp.trust_xheaders=True \
  --ServerApp.disable_check_xsrf=True --ServerApp.allow_remote_access=True
