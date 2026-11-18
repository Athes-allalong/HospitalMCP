FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hospital_mcp ./hospital_mcp
COPY web ./web
COPY scripts ./scripts
RUN useradd --create-home app && chown -R app /app
USER app
# The same image runs either service:
#   MCP server : python -m hospital_mcp.mcp_server        (default)
#   web portal : gunicorn -w 1 --threads 8 -b 0.0.0.0:$PORT "web.app:create_app()"
ENV MCP_HOST=0.0.0.0
CMD ["python", "-m", "hospital_mcp.mcp_server"]
