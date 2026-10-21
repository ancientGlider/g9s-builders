#!/bin/sh
# GIT_ASKPASS helper: answers git's credential prompts from the environment (pull mode).
case "$1" in
  *sername*) printf '%s\n' "${G9S_GIT_USER:-}" ;;
  *) printf '%s\n' "${G9S_GIT_PASSWORD:-}" ;;
esac
