#!/usr/bin/env bash
# Wrapper so deployall can be invoked from Git Bash / WSL bash.
# Delegates to deployall.ps1 via powershell.exe.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W 2>/dev/null || pwd)"
powershell.exe -ExecutionPolicy Bypass -File "${SCRIPT_DIR}/deployall.ps1"
