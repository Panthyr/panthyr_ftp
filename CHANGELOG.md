# Changelog

## v1.1.0

- Use `ftp_working_dir` from `settings` table to store files on server. Creates the directory if it does not exist.

## v1.1.1

- Uploads the files in a year subfolder on the server.

## v1.1.2

- Better exception handling:

  - Don't log as exception
  - Provide clear exception argument and re-raise

- Replace `setup.cfg` and `setup.py` with `pyproject.toml`
- Add makefile
