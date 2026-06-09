FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY apps/dashboard ./apps/dashboard
RUN pip install --no-cache-dir -e ".[dashboard]"

EXPOSE 8501
CMD ["streamlit", "run", "apps/dashboard/streamlit_app.py", "--server.address=0.0.0.0"]
