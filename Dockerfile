# Game Librarian for Azure Container Apps: one image for the website and for the scheduled sync job.
#   website (default):  python -m game_librarian serve --host 0.0.0.0 --port 8000   (container port 8000)
#   sync job:           set the job's arguments to   sync   (DEPLOYMENT.md)
# The catalog lives in PostgreSQL (GAME_LIBRARIAN_DATABASE_URL and PGPASSWORD, set as secrets in Azure): the
# container keeps no data of its own, so a restart or a new revision loses nothing. curated/ is part of the image;
# change it in Git and publish a new image. No secret, .env file or SQLite catalog is ever copied in (.dockerignore).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GAME_LIBRARIAN_HOSTED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY game_librarian ./game_librarian
COPY curated ./curated

# Run as an unprivileged user that owns no file in the image: the app only reads it.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin gamelibrarian
USER 10001

EXPOSE 8000
ENTRYPOINT ["python", "-m", "game_librarian"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
