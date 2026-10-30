"""Source entry point; packaged executable uses the same application."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from media_tool.app import main

if __name__ == "__main__":
    raise SystemExit(main())
