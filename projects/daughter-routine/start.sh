#!/bin/bash
cd /root/.openclaw/workspace/projects/daughter-routine
source venv/bin/activate
exec gunicorn app:app \
  --bind 0.0.0.0:5002 \
  --workers 2 \
  --timeout 60 \
  --access-logfile logs/access.log \
  --error-logfile logs/error.log \
  --capture-output \
  --daemon \
  --pid logs/gunicorn.pid
