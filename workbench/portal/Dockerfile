FROM --platform=linux/amd64 python:3.11-slim
RUN pip install --no-cache-dir aiohttp snowflake-connector-python
WORKDIR /app
COPY app.py pkgrepo.py ./
COPY static ./static
EXPOSE 8080
CMD ["python", "app.py"]
