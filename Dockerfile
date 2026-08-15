FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir inkbox
COPY server.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
