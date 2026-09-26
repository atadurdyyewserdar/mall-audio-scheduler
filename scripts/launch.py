"""PyInstaller entry point.

mall_audio/app.py uses relative imports, so it cannot be run as a top-level
script by the frozen bootloader. This shim imports it as part of its package.
"""
from mall_audio.app import main

if __name__ == "__main__":
    main()
