#! /usr/bin/env bash
set -e

git pull
docker build -t zhi-archive:latest -f BaseDockerfile .
