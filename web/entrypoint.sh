#!/bin/bash
set -e

echo "Applying migrations and seeding teams (waits for any other container doing the same)..."
python manage.py prepare_database

echo "Starting application..."
exec "$@"
