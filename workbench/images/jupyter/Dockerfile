FROM --platform=linux/amd64 quay.io/jupyter/minimal-notebook:2025-03-17

USER ${NB_UID}
RUN pip install --no-cache-dir aiohttp pandas matplotlib seaborn scikit-learn \
      "snowflake-connector-python[pandas]" snowflake-snowpark-python
USER root
COPY common/proxy.py /opt/wb/proxy.py
COPY jupyter/sfwb.py /opt/wb/sfwb.py
COPY jupyter/start-wb.sh /opt/wb/start-wb.sh
RUN chmod +x /opt/wb/start-wb.sh

# Runs as root so docker-stacks start.sh can fix ownership, then drops to jovyan
ENV WB_UPSTREAM=http://127.0.0.1:8888 PYTHONPATH=/opt/wb WB_GID=100
EXPOSE 8080
# Bypass docker-stacks ENTRYPOINT (it re-runs CMD as jovyan); start-wb.sh starts the proxy as root
# and then hands off to start.sh, which drops to jovyan for JupyterLab.
ENTRYPOINT ["tini", "-g", "--"]
CMD ["/opt/wb/start-wb.sh"]
