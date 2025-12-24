# VSIX Downloader (Marketplace `vspackage` URL helper)

A small, dependency-free Python tool that constructs **Visual Studio Code Marketplace** `vspackage` URLs and downloads extension packages as **installable `.vsix`** files into the current directory (or a directory you choose).

This is useful because the Marketplace web UI may not expose a direct “Download Extension” button anymore, while the underlying `.../vspackage` endpoint can still be used directly.

---

## Credits

This repository’s overall approach (using the Marketplace `vspackage` endpoint and the optional `targetPlatform` query parameter) is based on community findings documented in this StackOverflow thread:

- https://stackoverflow.com/questions/79359919/how-can-i-download-vsix-files-now-that-the-visual-studio-code-marketplace-no-lo

Implementation, structure, and code in this repo are original to this project unless explicitly stated otherwise.

---

## What this tool produces

A **VSIX is a ZIP archive** with a `.vsix` extension.

To make the output reliably installable, this tool uses the following pipeline:

1. Open the `vspackage` URL and **resolve the final output filename from HTTP response headers** (if available).
2. Download the raw payload to a temporary file: `*.vsix.download`
3. Normalize the payload into ZIP bytes:
   - handles gzip-encoded payloads defensively (even though the script requests identity encoding)
4. Optionally re-pack the normalized ZIP into a fresh ZIP container (`repack=True` by default)
5. Write the final artifact as `*.vsix` and remove temporary files

If the endpoint returns something that is not a VSIX/ZIP (e.g. HTML error page due to wrong version/platform),
the tool fails with a clear error instead of producing a broken file.

---

## Features

- ✅ Build Marketplace `vspackage` URLs from:
  - `publisher.extensionName`
  - `version`
  - optional `targetPlatform`
- ✅ Download one or many extensions
- ✅ Server-aware naming:
  - uses `Content-Disposition` filename when present (sanitized)
  - otherwise uses a deterministic fallback name
- ✅ Produces **installable `.vsix`** artifacts by:
  - normalizing payloads into ZIP format (including gzip decode if required)
  - optionally repacking into a clean ZIP container
  - atomic writes (`.part` file then rename)
- ✅ Library-friendly design:
  - no global logging configuration
  - functions are decoupled and testable (injectable opener/logger)

---

## Requirements

- Python 3.10+ (standard library only)

---

## Usage

### 1) Add extensions to download

In `vsix-downloader.py`, edit the list of `VsixSpec` entries (these are examples — replace with your own):

```py
EXTENSIONS: list[VsixSpec] = [
    VsixSpec("ms-vscode.cpptools", "1.30.0", "linux-x64"),
    # VsixSpec("somepublisher.someextension", "1.2.3", None),  # example "Universal" (omit targetPlatform)
]
```

* `unique_identifier` is `publisher.extensionName` (example: `ms-vscode.cpptools`)
* `version` must match the Marketplace version you want
* `target_platform` is optional:

  * use `"linux-x64"`, `"darwin-arm64"`, `"win32-x64"`, etc.
  * use `None` for *Universal* extensions (omits the `targetPlatform` query parameter)

### 2) Run as a script

Downloads all specs listed in `EXTENSIONS` into the current working directory:

```bash
python3 ./vsix-downloader.py
```

You’ll see progress/info messages via logging.

---

## Using it as a library

You can also import the module and call the functions directly:

```py
from pathlib import Path
from vsix_downloader import VsixSpec, download_vsix, download_many

spec = VsixSpec("ms-vscode.cpptools", "1.30.0", "linux-x64")
path = download_vsix(spec, dest_dir=Path("./downloads"))

paths = download_many(
    [
        VsixSpec("ms-vscode.cpptools", "1.30.0", "linux-x64"),
        VsixSpec("somepublisher.someextension", "1.2.3", "linux-x64"),
    ],
    dest_dir=Path("./downloads"),
)
print(paths)
```

### Repacking behaviour

By default, the tool re-packs the normalized ZIP bytes into a fresh ZIP container:

```py
download_vsix(spec, repack=True)   # default
```

You can disable repacking if you want to keep the normalized ZIP as-is:

```py
download_vsix(spec, repack=False)
```

---

## Logging

This module does not configure global logging by default (library-friendly).

For quick CLI usage, enable basic logging:

```py
from vsix_downloader import configure_basic_logging
configure_basic_logging()
```

Or configure logging however you prefer in your application.

---

## Install a downloaded VSIX

Once you have a `.vsix` file:

```bash
code --install-extension ./my-extension.vsix
```

or for VSCodium

```bash
codium --install-extension ./my-extension.vsix
```

---

## Troubleshooting

### “Not a valid VSIX” / “The file doesn’t install”

If the tool raises an error saying the payload is not a ZIP/VSIX after normalization, the request likely returned a non-VSIX response.

Common causes:

* wrong `version`
* wrong `targetPlatform`
* extension is “Universal” but you forced a platform
* Marketplace endpoint behaviour changed

What to do:

* verify the version string you’re requesting (Marketplace version history)
* try `target_platform=None` for Universal extensions
* run with logging enabled to see the exact URL being requested

### Temporary files left behind

The tool attempts best-effort cleanup. If the process is interrupted, you may see:

* `*.vsix.download`
* `*.vsix.normalized.zip`
* `*.part`

These can be safely deleted.

---

## Project layout

This repository is intentionally small:

```
.
├── vsix-downloader.py
├── README.md
└── LICENSE   (Apache-2.0)
```

---

## License

Apache License 2.0 — see `LICENSE`.

---

## Disclaimer

This project relies on publicly reachable Marketplace endpoints. Microsoft may change or restrict these APIs in the future. If downloads stop working, check current Marketplace behaviour and update the URL / request logic accordingly.