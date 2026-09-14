import os
import zipfile
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from core.config import settings

# Supported extensions mapped to programming language
LANGUAGE_EXTENSIONS: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".scala": "scala",
    ".kt": "kotlin",
}

# Directories that must be ignored
IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
}

# Obviously non-source extensions
IGNORED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pyc", ".pyo", ".pyd",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi",
}


class RepositoryServiceError(Exception):
    """Base exception for repository scanning and extraction errors."""
    pass


class SafeExtractor:
    """Safe ZIP archive extractor with Zip Slip / path traversal protection

    and file count limits.
    """

    @staticmethod
    def extract_zip(zip_path: Path, target_dir: Path, max_files: int = settings.MAX_FILES_COUNT) -> Path:
        if not zip_path.exists():
            raise RepositoryServiceError(f"ZIP archive not found at '{zip_path}'")

        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_target = target_dir.resolve()

        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            if len(namelist) > max_files:
                raise RepositoryServiceError(
                    f"ZIP contains {len(namelist)} items, exceeding maximum allowed limit of {max_files}"
                )

            for member in zf.infolist():
                # Security: prevent path traversal (Zip Slip)
                member_path = (target_dir / member.filename).resolve()
                if not str(member_path).startswith(str(resolved_target)):
                    raise RepositoryServiceError(
                        f"Malicious archive detected: '{member.filename}' attempts path traversal outside target directory"
                    )

                # Avoid extracting absolute or parent references
                if os.path.isabs(member.filename) or ".." in member.filename.split(os.path.sep):
                    raise RepositoryServiceError(
                        f"Unsafe path in ZIP archive: '{member.filename}'"
                    )

            # Safely extract all members
            zf.extractall(target_dir)

        return target_dir


class RepositoryScanner:
    """Recursively scans an extracted repository directory for supported source files."""

    @staticmethod
    def is_binary_file(file_path: Path) -> bool:
        """Heuristic to detect binary files (checks for null bytes in first 1024 bytes)."""
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
                return b"\x00" in chunk
        except Exception:
            return True

    @classmethod
    def scan_directory(
        cls,
        base_dir: Path,
        max_file_size_kb: int = settings.MAX_SOURCE_FILE_SIZE_KB,
    ) -> List[Dict[str, str]]:
        """Recursively scans base_dir for reviewable source code files.

        Returns a list of dicts:
            [{ "file_path": "rel/path.py", "full_path": "/abs/...", "language": "python" }, ...]
        """
        reviewable_files = []
        max_file_bytes = max_file_size_kb * 1024
        resolved_base = base_dir.resolve()

        for root, dirs, files in os.walk(resolved_base):
            # Modify dirs in-place to prevent walking into ignored directories
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]

            for filename in files:
                file_path = Path(root) / filename
                suffix = file_path.suffix.lower()

                # 1. Check supported code extension
                if suffix not in LANGUAGE_EXTENSIONS:
                    continue

                # 2. Skip ignored extensions
                if suffix in IGNORED_EXTENSIONS:
                    continue

                # 3. Check individual file size limit
                try:
                    size = file_path.stat().st_size
                    if size == 0 or size > max_file_bytes:
                        continue
                except OSError:
                    continue

                # 4. Binary check
                if cls.is_binary_file(file_path):
                    continue

                relative_path = file_path.relative_to(resolved_base).as_posix()
                reviewable_files.append({
                    "file_path": relative_path,
                    "full_path": str(file_path),
                    "language": LANGUAGE_EXTENSIONS[suffix],
                })

        return reviewable_files
