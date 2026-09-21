# Darktide Mod History

Find Darktide mod repositories on GitHub and render their combined Git history with Gource.

## Run

Requires Python 3.11+, Git, Gource, FFmpeg (including ffprobe), and a GitHub token for Code Search. The command-line tools must be on PATH.

Run from this checkout in PowerShell:

```powershell
$env:GITHUB_TOKEN = '...'
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
darktide-mod-history all
```

Edit `config.toml` for discovery and rendering settings, and `game-updates.toml` for game-update captions.

The video is saved to `output/darktide-mod-history.mp4` by default. Downloads and intermediate files are also stored under `output/` and excluded from Git.

Repository discovery depends on GitHub's search index and may miss repositories.
