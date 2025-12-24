# VSIX Downloader (Marketplace `vspackage` URL helper)

A small, dependency-free Python tool that constructs **Visual Studio Code Marketplace** `vspackage` URLs and downloads extension packages (`.vsix`) into the current directory (or a directory you choose).

This is handy because the Marketplace web UI may not expose a direct “Download Extension” button anymore, while the underlying `.../vspackage` endpoint can still be used directly.

---

## Credits

This repository’s approach (specifically: using the Marketplace `vspackage` endpoint and the `targetPlatform` query parameter) is based on community findings documented in this StackOverflow thread:

- StackOverflow: “How can I download vsix files now that the Visual Studio Code Marketplace no longer…”  
  https://stackoverflow.com/questions/79359919/how-can-i-download-vsix-files-now-that-the-visual-studio-code-marketplace-no-lo

Implementation, structure, and code in this repo are original to this project unless explicitly stated otherwise.

---

## Features

- ✅ Build Marketplace `vspackage` URLs from:
  - `publisher.extensionName`
  - `version`
  - optional `targetPlatform`
- ✅ Download one or many VSIX files
- ✅ Library-friendly design:
  - no global logging configuration
  - functions are decoupled and testable (injectable opener/logger)
- ✅ Robust output naming:
  - uses server-provided filename if available
  - sanitizes name for safety
  - forces `.vsix` suffix (VSIX is a zip container; some tools label it `.zip`)
- ✅ Atomic writes (`.part` file then rename)

---

## Requirements

- Python 3.10+ (standard library only)

---

## Usage

### 1) Add extensions to download

In `vsix_downloader.py`, edit the list of `VsixSpec` entries (these are **examples** — replace with your own):

```py
EXTENSIONS: list[VsixSpec] = [
    VsixSpec("ms-vscode.cpptools", "1.30.0", "linux-x64"),
    # VsixSpec("ms-python.python", "2025.1.0", None),  # example "Universal" (omit targetPlatform)
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
python3 vsix_downloader.py
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
        VsixSpec("somepublisher.someextension", "1.2.3", "linux-x64"),  # example
    ],
    dest_dir=Path("./downloads"),
)
print(paths)
```

### Logging

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

(Use `code-insiders` if you run VS Code Insiders.)

---

## Project layout

This repository is intentionally small:

```
.
├── vsix_downloader.py
├── README.md
└── LICENSE   (Apache-2.0)
```

---

## License

Apache License 2.0 — see `LICENSE`.

---

## Disclaimer

This project relies on publicly reachable Marketplace endpoints. Microsoft may change or restrict these APIs in the future. If downloads stop working, check current Marketplace behavior and update the URL / request logic accordingly.

