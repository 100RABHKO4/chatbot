FROM python:3.11-slim
WORKDIR /app
COPY *.py ./
COPY strategies ./strategies
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "python server.py --host 0.0.0.0 --port ${PORT}"]
