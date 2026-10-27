# Pinned by digest, so a rebuild or a rollback gets the same base rather than
# whatever 3.12-slim pointed at that day. Bump tag and digest together.
FROM python:3.12.16-slim-trixie@sha256:962f801b08c870ac2a51b72bf02dae38c9a4162319e5e882ec244e198188d54e

WORKDIR /app
# requirements.lock is requirements.txt resolved for Linux with every hash (see
# the README to regenerate). --require-hashes refuses anything that is not
# exactly what was locked, so a build cannot quietly take a new or swapped package.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY . .

# The app writes nothing to its own files, so it has no use for root; a
# compromised process then does not own the container's filesystem.
RUN useradd --create-home --uid 10001 app
USER app

# A persistent deployment has no separate pre-deploy hook to run migrations
# in, so upgrade-then-serve has to be one command. Local dev runs
# `alembic upgrade head` by hand instead, per the README, so this only
# changes what a built image does on its own.
#
# `exec` before uvicorn replaces the shell with it, so uvicorn becomes the
# container's PID 1 and receives SIGTERM directly. Without it, `docker stop`
# signals a shell that does not forward it, uvicorn never sees the shutdown,
# and every stop or redeploy waits out the full timeout and gets killed
# instead of shutting down cleanly.
#
# PORT comes from the host when it assigns one (Railway does); 8000 otherwise.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
