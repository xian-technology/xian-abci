# Scripts

## Purpose

This folder contains the thin script surface that exists outside the importable
runtime code.

## Contents

- `validate-repo.sh`: the preferred full validation entrypoint for local and CI
  use

## Notes

- Keep this folder deliberately small.
- Prefer importable helpers in `src/xian/` for reusable logic, and keep shell
  scripts here only when they are truly repo-level maintenance entrypoints.
