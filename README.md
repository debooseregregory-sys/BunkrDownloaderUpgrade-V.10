# Bunkr Downloader — Kid Acid Edition

**Kid Acid Edition · V. 1.0.0**

Bunkr Downloader is an upgraded and modified edition of an existing Bunkr downloader. It focuses on reliable downloads, safe file handling, and clear progress tracking. This edition modifies existing software; it does not claim authorship of the original Bunkr downloader.

Project: <https://github.com/debooseregregory-sys/BunkrDownloaderUpgrade-V.10>

## Features

- Download a single Bunkr file or an album.
- Queue multiple album and file URLs, or load a batch from a text file.
- Show per-item queue status and download progress.
- Skip completed local files marked **SKIPPED — ALREADY EXISTS**; existing files are not silently overwritten.
- Write downloads to a temporary `.bunkr-part` file and publish the final file only after the download has been verified.
- Retry recoverable network/server errors with controlled waits and fresh resolve/signing steps when required.
- Choose the number of simultaneous downloads in Settings.
- Optionally shut down Windows after the entire queue finishes successfully (or items are skipped because they already exist). The option is off by default, failed items require confirmation, and the 30-second countdown can be cancelled.

Scanning and resolving files does not delete, move, rename, or overwrite user files. Downloads that already exist locally are skipped.

## Download statuses

The queue reports each URL as it is processed, including **Waiting**, **Downloading**, **Completed**, **Skipped**, **Retrying**, **Failed**, and **Unverified** when the external-console mode cannot report per-file completion to the GUI.

A download is only marked complete after its final file is verified and published. A triggered or attempted download is not treated as a completed local file.

## HTTP 503 and unstable downloads

If downloads fail with HTTP 503 errors or are unstable, set **Max concurrent downloads** to **1** in Settings. A CDN may limit concurrent requests; reducing this setting can make downloads more reliable.

## Run from source

Use Python 3.11 or 3.12 on Windows. From the project directory, create and activate a virtual environment, install the project requirements and the GUI package, then start `gui.py`:

```bat
py -3.12 -m venv venv
call venv\Scripts\activate.bat
python -m pip install -r requirements.txt customtkinter
python gui.py
```

To run the command-line downloader instead:

```bat
python downloader.py <bunkr_url>
```

## Portable Windows executable

The portable executable can be built from source with the project build script:

```bat
build_modern.bat
```

The script uses `BunkrDownloaderPortableGUI_Modern.spec` and writes the executable to `dist\BunkrDownloaderPortableGUI_Modern.exe`. The build bundles the application and its runtime; the target computer does not need the development virtual environment.

## License and credits

This modified edition retains the project license and notices in [LICENSE](LICENSE). The underlying downloader is based on existing open-source software; credit remains with its original contributors. Kid Acid branding refers to this modified edition only.
