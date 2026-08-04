FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

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
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
