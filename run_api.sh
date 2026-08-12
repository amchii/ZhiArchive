#! /usr/bin/env bash

api_host="${1:-0.0.0.0}"
api_port="${2:-9090}"

uvicorn archive.api.app:app --host "$api_host" --port "$api_port" --workers 1
