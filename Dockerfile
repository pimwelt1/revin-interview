# SummitAir voice agent: one process on one machine, because call state lives in memory
# (InMemorySaver) and bookings live in CSV files guarded by an in-process lock.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
# technicians.csv is not in git, so a copy ships in the image and seeds an empty volume on first boot.
COPY data/technicians.csv seed/technicians.csv

# The volume is the working directory, so both data/ and logs/ land on it and survive a deploy.
WORKDIR /data
EXPOSE 8080
CMD ["sh", "-c", "cp -n /app/seed/technicians.csv /data/technicians.csv; \
exec uvicorn voice_agent.main:app --host 0.0.0.0 --port 8080"]
