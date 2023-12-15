#! /usr/bin/env bash

uvicorn archive.api.app:app --host 0.0.0.0 --port 9090
