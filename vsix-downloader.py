from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import unquote
from urllib.request import Request, urlopen


# =============================================================================
# Library-friendly logging setup
# =============================================================================

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())  # prevents "No handler found" warnings in libraries


def configure_basic_logging(level: int = logging.INFO) -> None:
    """
    Convenience helper for CLI scripts.

    Libraries should NOT configure global logging by default.
    If you're using this module in a script, call this once to see INFO messages.

    Example:
        configure_basic_logging()
    """
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


# =============================================================================
# Data model
# =============================================================================

MARKETPLACE_VSPACKAGE_BASE = "https://marketplace.visualstudio.com/_apis/public/gallery"


@dataclass(frozen=True)
class VsixSpec:
    """
    Specification for a VS Code extension VSIX download.

    Attributes:
        unique_identifier:
            Extension identifier in the form "publisher.extensionName"
            (e.g. "ms-vscode.cpptools").
        version:
            Extension version string (e.g. "1.30.0").
        target_platform:
            Optional marketplace target platform (e.g. "linux-x64").
            Use None to omit the query parameter (commonly for "Universal" extensions).
    """
    unique_identifier: str
    version: str
    target_platform: str | None = "linux-x64"


# =============================================================================
# URL construction
# =============================================================================

def build_vspackage_url(spec: VsixSpec, base: str = MARKETPLACE_VSPACKAGE_BASE) -> str:
    """
    Build the Marketplace 'vspackage' URL for the given extension spec.

    The returned URL is directly downloadable and typically returns the VSIX payload.

    Args:
        spec: Extension specification.
        base: Base URL of the Marketplace gallery API.

    Returns:
        A fully qualified URL to the extension 'vspackage' endpoint.

    Raises:
        ValueError: If spec.unique_identifier is not "publisher.package".
    """
    publisher, package = spec.unique_identifier.split(".", 1)
    url = f"{base}/publishers/{publisher}/vsextensions/{package}/{spec.version}/vspackage"
    if spec.target_platform:
        url += f"?targetPlatform={spec.target_platform}"
    return url


# =============================================================================
# Filename handling
# =============================================================================

_CD_FILENAME_RE = re.compile(r'filename="?(?P<name>[^";]+)"?', re.IGNORECASE)


def filename_from_content_disposition(header: str | None) -> str | None:
    """
    Extract a filename from a Content-Disposition header.

    Supports common forms:
        - filename="x"
        - filename=x
        - filename*=UTF-8''<urlencoded>

    Args:
        header: The Content-Disposition header value.

    Returns:
        The extracted filename, or None if not present.
    """
    if not header:
        return None

    # Prefer RFC 5987 / 6266 form: filename*=UTF-8''...
    parts = [p.strip() for p in header.split(";")]
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().lower()] = v.strip().strip('"')

    if "filename*" in params:
        v = params["filename*"]
        if "''" in v:
            _, enc = v.split("''", 1)
            return unquote(enc)
        return unquote(v)

    # Fall back to filename=
    m = _CD_FILENAME_RE.search(header)
    if m:
        return m.group("name")

    return None


def safe_filename(name: str) -> str:
    """
    Make a filename safe to use as a single path segment.

    This prevents server-provided names from accidentally creating directories.

    Args:
        name: Candidate filename.

    Returns:
        A sanitized filename (single path segment).
    """
    # Keep only the final path component and strip common dangerous characters.
    return Path(name).name.replace("\x00", "").strip() or "download.vsix"


def default_vsix_name(spec: VsixSpec) -> str:
    """
    Build a deterministic fallback name when the server doesn't provide one.

    Args:
        spec: Extension specification.

    Returns:
        A stable filename ending with .vsix.
    """
    plat = f"-{spec.target_platform}" if spec.target_platform else ""
    return f"{spec.unique_identifier}-{spec.version}{plat}.vsix"


def ensure_vsix_suffix(path: Path) -> Path:
    """
    Ensure a path ends with a .vsix suffix.

    The Marketplace may return a payload that browsers/tools label as .zip.
    A VSIX is a zip container, so renaming to .vsix is typically sufficient.

    Args:
        path: Output file path.

    Returns:
        Path with .vsix suffix.
    """
    suf = path.suffix.lower()
    if suf == ".vsix":
        return path
    if suf == ".zip":
        return path.with_suffix(".vsix")
    return path.with_suffix(".vsix")


# =============================================================================
# Download mechanics
# =============================================================================

def iter_response_chunks(resp, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """
    Iterate a response body in chunks.

    Args:
        resp: A file-like HTTP response object with .read().
        chunk_size: Chunk size in bytes.

    Yields:
        Byte chunks until EOF.
    """
    while True:
        chunk = resp.read(chunk_size)
        if not chunk:
            break
        yield chunk


def atomic_write_bytes(dest: Path, data_iter: Iterable[bytes]) -> int:
    """
    Atomically write streamed bytes to disk using a temporary '.part' file.

    Args:
        dest: Final destination path.
        data_iter: Iterable yielding byte chunks.

    Returns:
        Total number of bytes written.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)

    with open(tmp, "wb") as f:
        for chunk in data_iter:
            f.write(chunk)
            total += len(chunk)

    tmp.replace(dest)
    return total


def download_vsix(
    spec: VsixSpec,
    dest_dir: Path | None = None,
    *,
    user_agent: str = "vsix-downloader/1.0 (+python urllib)",
    opener: Callable[[Request], object] = urlopen,
    log: Optional[logging.Logger] = None,
) -> Path:
    """
    Download a VSIX specified by `spec` into `dest_dir` (default: current working directory).

    Behaviour:
      - Builds the Marketplace vspackage URL.
      - Requests the file (following redirects handled by urllib).
      - Chooses the output name from Content-Disposition if available; otherwise a fallback.
      - Sanitizes the filename.
      - Forces the output extension to .vsix.
      - Writes atomically (download to .part then rename).

    Args:
        spec: Extension spec to download.
        dest_dir: Destination directory. Defaults to Path.cwd().
        user_agent: User-Agent header to send.
        opener: Injectable opener for testability (defaults to urllib.request.urlopen).
        log: Optional logger (defaults to this module's logger).

    Returns:
        Path to the downloaded file.

    Raises:
        URLError / HTTPError: On network or HTTP failures (from urllib).
        OSError: On file system errors.
        ValueError: If spec.unique_identifier is malformed.
    """
    log = log or logger
    dest_dir = dest_dir or Path.cwd()

    url = build_vspackage_url(spec)
    log.info("Preparing download: %s@%s", spec.unique_identifier, spec.version)
    log.info("URL: %s", url)

    req = Request(url, headers={"User-Agent": user_agent})

    with opener(req) as resp:  # type: ignore[call-arg]
        cd = getattr(resp, "headers", {}).get("Content-Disposition") if hasattr(resp, "headers") else None
        server_name = filename_from_content_disposition(cd)
        chosen_name = safe_filename(server_name) if server_name else default_vsix_name(spec)

        out_path = ensure_vsix_suffix(dest_dir / chosen_name)
        log.info("Saving to: %s", out_path)

        total = atomic_write_bytes(out_path, iter_response_chunks(resp))
        log.info("Done (%d bytes): %s", total, out_path)

    return out_path


def download_many(
    specs: Iterable[VsixSpec],
    dest_dir: Path | None = None,
    *,
    user_agent: str = "vsix-downloader/1.0 (+python urllib)",
    opener: Callable[[Request], object] = urlopen,
    log: Optional[logging.Logger] = None,
) -> list[Path]:
    """
    Download multiple VSIX files.

    This is a thin orchestration layer around `download_vsix()`, keeping the
    underlying download logic reusable and testable.

    Args:
        specs: Iterable of extension specs.
        dest_dir: Destination directory (defaults to CWD).
        user_agent: User-Agent header.
        opener: Injectable opener for testability.
        log: Optional logger.

    Returns:
        List of paths to downloaded files.
    """
    log = log or logger
    results: list[Path] = []

    specs_list = list(specs)
    log.info("Starting batch download (%d extension(s))", len(specs_list))

    for i, spec in enumerate(specs_list, start=1):
        log.info("(%d/%d) %s", i, len(specs_list), spec.unique_identifier)
        results.append(
            download_vsix(
                spec,
                dest_dir=dest_dir,
                user_agent=user_agent,
                opener=opener,
                log=log,
            )
        )

    log.info("Batch download complete (%d file(s))", len(results))
    return results


# =============================================================================
# Example CLI usage
# =============================================================================

if __name__ == "__main__":
    configure_basic_logging()

    EXTENSIONS: list[VsixSpec] = [
        VsixSpec("ms-vscode.cpptools", "1.30.0", "linux-x64"),
        # VsixSpec("platformio.platformio-ide", "3.3.4", "linux-x64"),
    ]

    download_many(EXTENSIONS)
