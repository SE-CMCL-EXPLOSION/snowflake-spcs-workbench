ARG R_VERSION=4.4.3
FROM --platform=linux/amd64 rocker/rstudio:${R_VERSION}

# Snowflake ODBC driver + unixODBC + Python proxy deps
RUN apt-get update && apt-get install -y --no-install-recommends \
      unixodbc unixodbc-dev curl python3 python3-aiohttp libxml2-dev libcurl4-openssl-dev libssl-dev \
 && curl -fsSL -o /tmp/sf.deb https://sfc-repo.snowflakecomputing.com/odbc/linux/3.21.0/snowflake-odbc-3.21.0.x86_64.deb \
 && (dpkg -i /tmp/sf.deb || apt-get -fy install) && rm /tmp/sf.deb \
 && sed -i 's#libodbcinst.so.1#libodbcinst.so.2#' /usr/lib/snowflake/odbc/lib/simba.snowflake.ini \
 && rm -rf /var/lib/apt/lists/*

RUN Rscript -e 'install.packages(c("DBI","odbc","dplyr","dbplyr","ggplot2","tidyr","paws","rstudioapi","BiocManager"))' \
 && Rscript -e 'BiocManager::install("limma", update = FALSE, ask = FALSE)'

COPY common/proxy.py /opt/wb/proxy.py
COPY rstudio/Rprofile.site /opt/wb/Rprofile.site
COPY rstudio/start.sh /opt/wb/start.sh
RUN cat /opt/wb/Rprofile.site >> /usr/local/lib/R/etc/Rprofile.site && chmod +x /opt/wb/start.sh \
 && echo 'session-default-working-dir=~/work' >> /etc/rstudio/rsession.conf

# Portal + SPCS ingress already authenticated the user
ENV DISABLE_AUTH=true WB_UPSTREAM=http://127.0.0.1:8787
EXPOSE 8080
CMD ["/opt/wb/start.sh"]
