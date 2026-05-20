"""Shared application constants."""

# File upload connections (CSV, TSV, Excel) — per-file limit on disk.
FILE_UPLOAD_MAX_BYTES = 500 * 1024 * 1024  # 500 MB

# Maximum files accepted in one batch upload request.
FILE_UPLOAD_MAX_FILES_PER_BATCH = 20
