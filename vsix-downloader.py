from __future__ import annotations

import gzip
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional, Tuple
from urllib.parse import unquote
from urllib.request import Request, urlopen


# =============================================================================
# Library-friendly logging setup
# =============================================================================

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())  # avoids "No handler found" warnings in libraries


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

    Args:
        spec: Extension specification.
        base: Base URL of the Marketplace gallery API.

    Returns:
        A fully qualified URL to the extension 'vspackage' endpoint.

    Raises:
        ValueError: If spec.unique_identifier is not "publisher.extensionName".
    """
    publisher, package = spec.unique_identifier.split(".", 1)
    url = f"{base}/publishers/{publisher}/vsextensions/{package}/{spec.version}/vspackage"
    if spec.target_platform:
        url += f"?targetPlatform={spec.target_platform}"
    return url


# =============================================================================
# Header + filename handling
# =============================================================================

_CD_FILENAME_RE = re.compile(r'filename="?(?P<name>[^";]+)"?', re.IGNORECASE)


def _header_get(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive mapping access for HTTP headers."""
    needle = name.lower()
    for k, v in headers.items():
        if k.lower() == needle:
            return v
    return None


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


def resolve_vsix_filename(spec: VsixSpec, headers: Mapping[str, str]) -> str:
    """
    Decide the final VSIX filename.

    Preference order:
      1) Server-provided Content-Disposition filename (sanitized)
      2) Deterministic fallback derived from spec

    Returns:
        A filename that ends with `.vsix`.
    """
    cd = _header_get(headers, "Content-Disposition")
    server_name = filename_from_content_disposition(cd)
    chosen = safe_filename(server_name) if server_name else default_vsix_name(spec)
    return ensure_vsix_suffix(Path(chosen)).name


# =============================================================================
# Streaming / atomic I/O utilities
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


def iter_file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Iterate a file on disk in fixed-size chunks."""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
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


def _read_prefix(path: Path, n: int = 16) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def is_zip_file(path: Path) -> bool:
    """
    Return True if the file at `path` is a ZIP container.

    Args:
        path: Path to a file.

    Returns:
        True if ZIP, False otherwise.
    """
    return zipfile.is_zipfile(path)


# =============================================================================
# Download + VSIX production pipeline
# =============================================================================

def download_vspackage_payload(
    url: str,
    *,
    spec: VsixSpec,
    dest_dir: Path,
    user_agent: str,
    opener: Callable[[Request], object],
    log: logging.Logger,
) -> Tuple[Path, Path, Mapping[str, str]]:
    """
    Download the Marketplace payload into a `.download` file.

    Important detail:
      The output filename is resolved from response headers *before* streaming the body,
      so we never end up "switching" paths after the download.

    Args:
        url: Download URL (vspackage endpoint).
        spec: The extension spec.
        dest_dir: Destination directory.
        user_agent: User-Agent header value.
        opener: Injectable opener for testability.
        log: Logger instance.

    Returns:
        (final_vsix_path, raw_download_path, headers)

    Raises:
        URLError / HTTPError: from urllib on network/HTTP failures.
        OSError: on file system errors.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    req = Request(
        url,
        headers={
            "User-Agent": user_agent,
            # Request identity encoding to avoid gzip surprises where possible.
            # We still handle gzip defensively in normalize_to_zip().
            "Accept-Encoding": "identity",
        },
    )

    log.info("HTTP GET: %s", url)

    with opener(req) as resp:  # type: ignore[call-arg]
        # urllib headers are typically an email.message.Message which supports .items()
        hdrs: Mapping[str, str] = dict(getattr(resp, "headers", {}).items())

        final_name = resolve_vsix_filename(spec, hdrs)
        final_vsix = ensure_vsix_suffix(dest_dir / final_name)
        raw_download = final_vsix.with_suffix(final_vsix.suffix + ".download")

        log.info("Saving payload to: %s", raw_download)
        total = atomic_write_bytes(raw_download, iter_response_chunks(resp))
        log.info("Downloaded %d bytes -> %s", total, raw_download)

    return final_vsix, raw_download, hdrs


def normalize_to_zip(src: Path, dest: Path, headers: Mapping[str, str], *, log: logging.Logger) -> None:
    """
    Normalize a downloaded payload into a ZIP file.

    The Marketplace typically returns a VSIX (a ZIP container). In practice you may
    encounter:
      - gzip-wrapped payloads, or
      - non-zip responses (HTML error pages, wrong version/targetPlatform)

    This function:
      - detects gzip via Content-Encoding and magic bytes
      - decompresses if needed (streaming)
      - otherwise copies bytes as-is
      - validates ZIP-ness of the result

    Args:
        src: Downloaded payload path.
        dest: Destination path for the normalized ZIP bytes.
        headers: HTTP response headers.
        log: Logger.

    Raises:
        ValueError: If the normalized output is not a ZIP file.
    """
    content_encoding = (_header_get(headers, "Content-Encoding") or "").lower().strip()

    magic = _read_prefix(src, 2)
    is_gzip_magic = magic == b"\x1f\x8b"
    is_gzip_header = "gzip" in content_encoding

    if is_gzip_magic or is_gzip_header:
        log.info("Normalizing: detected gzip-encoded payload; decompressing")
        tmp = dest.with_suffix(dest.suffix + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(src, "rb") as zin, open(tmp, "wb") as zout:
            while True:
                chunk = zin.read(1024 * 1024)
                if not chunk:
                    break
                zout.write(chunk)
        tmp.replace(dest)
    else:
        log.info("Normalizing: payload is not gzip-encoded; copying as-is")
        atomic_write_bytes(dest, iter_file_chunks(src))

    if not is_zip_file(dest):
        head = _read_prefix(dest, 64)
        raise ValueError(
            "Downloaded payload is not a ZIP/VSIX file after normalization. "
            "This usually means the request returned an HTML error page "
            "(wrong version / wrong targetPlatform / endpoint changed). "
            f"First bytes: {head!r}"
        )


def repack_zip(src_zip: Path, dest_zip: Path, *, log: logging.Logger) -> None:
    """
    Re-pack an existing ZIP file into a fresh ZIP container.

    This is a pragmatic “make it definitely a normal ZIP” step. It can help when:
      - you want consistent compression,
      - you want to ensure the output is a conventional ZIP container.

    Args:
        src_zip: Source ZIP file.
        dest_zip: Destination ZIP file (often `*.vsix`).
        log: Logger.

    Raises:
        ValueError: If src_zip is not a ZIP file.
    """
    if not is_zip_file(src_zip):
        head = _read_prefix(src_zip, 64)
        raise ValueError(f"Cannot repack: source is not a ZIP file. First bytes: {head!r}")

    tmp = dest_zip.with_suffix(dest_zip.suffix + ".part")
    dest_zip.parent.mkdir(parents=True, exist_ok=True)

    log.info("Repacking ZIP -> VSIX container")
    with zipfile.ZipFile(src_zip, "r") as zin, zipfile.ZipFile(
        tmp, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = b"" if info.is_dir() else zin.read(info.filename)

            # Preserve essential metadata where practical.
            new_info = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            new_info.external_attr = info.external_attr
            new_info.internal_attr = info.internal_attr
            new_info.create_system = info.create_system
            new_info.create_version = info.create_version
            new_info.extract_version = info.extract_version
            new_info.flag_bits = info.flag_bits
            new_info.volume = info.volume
            new_info.comment = info.comment
            new_info.extra = info.extra

            zout.writestr(new_info, data)

    tmp.replace(dest_zip)


# =============================================================================
# Public API
# =============================================================================

def download_vsix(
    spec: VsixSpec,
    dest_dir: Path | None = None,
    *,
    repack: bool = True,
    user_agent: str = "vsix-downloader/1.0 (+python urllib)",
    opener: Callable[[Request], object] = urlopen,
    log: Optional[logging.Logger] = None,
) -> Path:
    """
    Download an extension package and produce an installable `.vsix`.

    Pipeline:
      1) Open URL and resolve the output filename from headers (before streaming)
      2) Download raw payload to `<name>.vsix.download`
      3) Normalize payload into ZIP bytes
      4) Optionally re-pack into a clean ZIP container
      5) Write final `*.vsix` and clean up temporary files

    Args:
        spec: Extension spec to download.
        dest_dir: Destination directory (defaults to current working directory).
        repack: If True (default), re-pack the normalized ZIP into a fresh container.
        user_agent: User-Agent header to send.
        opener: Injectable opener for testability (defaults to urllib.request.urlopen).
        log: Optional logger (defaults to this module's logger).

    Returns:
        Path to the resulting `.vsix` file.

    Raises:
        ValueError: If the final artifact cannot be made into a ZIP/VSIX container.
        URLError / HTTPError: On network or HTTP failures (from urllib).
        OSError: On file system errors.
    """
    log = log or logger
    dest_dir = dest_dir or Path.cwd()
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = build_vspackage_url(spec)
    log.info("Preparing: %s@%s", spec.unique_identifier, spec.version)

    final_vsix, raw_download, headers = download_vspackage_payload(
        url,
        spec=spec,
        dest_dir=dest_dir,
        user_agent=user_agent,
        opener=opener,
        log=log,
    )

    normalized_zip = final_vsix.with_suffix(final_vsix.suffix + ".normalized.zip")

    log.info("Validating/normalizing payload into a ZIP container")
    normalize_to_zip(raw_download, normalized_zip, headers, log=log)

    log.info("Producing final VSIX: %s", final_vsix)
    if repack:
        repack_zip(normalized_zip, final_vsix, log=log)
    else:
        tmp = final_vsix.with_suffix(final_vsix.suffix + ".part")
        atomic_write_bytes(tmp, iter_file_chunks(normalized_zip))
        tmp.replace(final_vsix)

    # Cleanup (best-effort)
    for p in (raw_download, normalized_zip):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove temporary file: %s", p)

    log.info("Done: %s", final_vsix)
    return final_vsix


def download_many(
    specs: Iterable[VsixSpec],
    dest_dir: Path | None = None,
    *,
    repack: bool = True,
    user_agent: str = "vsix-downloader/1.0 (+python urllib)",
    opener: Callable[[Request], object] = urlopen,
    log: Optional[logging.Logger] = None,
) -> list[Path]:
    """
    Download multiple extensions and produce installable `.vsix` files.

    Args:
        specs: Iterable of extension specs.
        dest_dir: Destination directory (defaults to CWD).
        repack: If True (default), re-pack each normalized ZIP into a fresh container.
        user_agent: User-Agent header.
        opener: Injectable opener for testability.
        log: Optional logger.

    Returns:
        List of paths to downloaded `.vsix` files.
    """
    log = log or logger
    specs_list = list(specs)
    log.info("Starting batch download (%d extension(s))", len(specs_list))

    results: list[Path] = []
    for i, spec in enumerate(specs_list, start=1):
        log.info("(%d/%d) %s", i, len(specs_list), spec.unique_identifier)
        results.append(
            download_vsix(
                spec,
                dest_dir=dest_dir,
                repack=repack,
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
        # VsixSpec("somepublisher.someextension", "1.2.3", "linux-x64"),
        # VsixSpec("somepublisher.universalextension", "1.2.3", None),
    ]

    root_dir = Path(__file__).resolve().parent
    vscode_exts_dir = root_dir / "vscode_exts/"
    download_many(EXTENSIONS, dest_dir=vscode_exts_dir)
