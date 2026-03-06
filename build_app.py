#!/usr/bin/env python3
"""
Build macOS .app bundle for X Bookmark Manager.

Creates /Applications/X Bookmark Manager.app with:
  - Info.plist (app metadata)
  - MacOS/launcher (shell script that runs dashboard.py)
  - Resources/AppIcon.icns (generated blue bookmark icon)

No bundling — the .app just points to the existing Python + project directory.
"""

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "X Bookmark Manager"
BUNDLE_ID = "com.mshrmnsr.x-bookmark-manager"
APP_DIR = Path(f"/Applications/{APP_NAME}.app")
PROJECT_DIR = Path(__file__).resolve().parent
PYTHON = "/opt/homebrew/bin/python3"


def create_icon(resources_dir):
    """Generate a blue bookmark icon and convert to .icns."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  Pillow not available, using fallback icon")
        create_fallback_icon(resources_dir)
        return

    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background: rounded blue square
    margin = 80
    radius = 180
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius,
        fill=(30, 100, 220),  # blue
    )

    # White "X" letter in the center
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 520)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/SFCompact.ttf", 520)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Draw "X" centered
    text = "X"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size - th) / 2 - bbox[1] - 20
    draw.text((tx, ty), text, fill=(255, 255, 255), font=font)

    # Small bookmark icon below the X
    bx, by = size // 2, size - margin - 160
    bw, bh = 60, 80
    draw.polygon(
        [(bx - bw, by - bh), (bx + bw, by - bh),
         (bx + bw, by + bh), (bx, by + bh - 30),
         (bx - bw, by + bh)],
        fill=(255, 255, 255, 200),
    )

    # Create iconset directory with required sizes
    with tempfile.TemporaryDirectory() as tmpdir:
        iconset = Path(tmpdir) / "AppIcon.iconset"
        iconset.mkdir()

        icon_sizes = [
            (16, "16x16", 1), (32, "16x16", 2),
            (32, "32x32", 1), (64, "32x32", 2),
            (128, "128x128", 1), (256, "128x128", 2),
            (256, "256x256", 1), (512, "256x256", 2),
            (512, "512x512", 1), (1024, "512x512", 2),
        ]
        for px, name, scale in icon_sizes:
            resized = img.resize((px, px), Image.LANCZOS)
            suffix = f"@2x" if scale == 2 else ""
            resized.save(iconset / f"icon_{name}{suffix}.png")

        # Convert to .icns using iconutil
        icns_path = resources_dir / "AppIcon.icns"
        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"  Icon created: {icns_path}")
        else:
            print(f"  iconutil failed: {result.stderr}")
            create_fallback_icon(resources_dir)


def create_fallback_icon(resources_dir):
    """Create a simple icon using sips if Pillow is unavailable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a simple 1024x1024 blue PNG using sips
        png_path = Path(tmpdir) / "icon.png"
        # Create with Python's built-in capabilities: a minimal PNG
        import struct
        import zlib

        width = height = 256
        # Blue pixels (RGBA)
        r, g, b, a = 30, 100, 220, 255
        raw_data = b""
        for _ in range(height):
            raw_data += b"\x00"  # filter byte
            raw_data += bytes([r, g, b, a]) * width

        def make_chunk(chunk_type, data):
            chunk = chunk_type + data
            return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xffffffff)

        png = b"\x89PNG\r\n\x1a\n"
        png += make_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        png += make_chunk(b"IDAT", zlib.compress(raw_data))
        png += make_chunk(b"IEND", b"")

        png_path.write_bytes(png)

        # Convert to iconset + icns
        iconset = Path(tmpdir) / "AppIcon.iconset"
        iconset.mkdir()

        for name in ["icon_16x16.png", "icon_16x16@2x.png", "icon_32x32.png",
                      "icon_32x32@2x.png", "icon_128x128.png", "icon_128x128@2x.png",
                      "icon_256x256.png", "icon_256x256@2x.png", "icon_512x512.png",
                      "icon_512x512@2x.png"]:
            shutil.copy2(str(png_path), str(iconset / name))

        icns_path = resources_dir / "AppIcon.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
            capture_output=True
        )
        print(f"  Fallback icon created: {icns_path}")


def create_info_plist(contents_dir):
    """Write Info.plist with app metadata."""
    plist = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": "launcher",
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,  # Show in Dock when running
        "NSSupportsAutomaticGraphicsSwitching": True,
    }
    plist_path = contents_dir / "Info.plist"
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)
    print(f"  Info.plist written: {plist_path}")


def create_launcher(macos_dir):
    """Write the shell launcher script."""
    launcher = macos_dir / "launcher"
    launcher.write_text(f"""#!/bin/bash
# X Bookmark Manager launcher
# Points to the project directory — no bundled Python

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "{PROJECT_DIR}"
exec "{PYTHON}" scripts/dashboard.py
""")
    # Make executable
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  Launcher written: {launcher}")


def register_app():
    """Register with Launch Services so Spotlight and Dock recognize it."""
    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework"
        "/Versions/A/Frameworks/LaunchServices.framework"
        "/Versions/A/Support/lsregister"
    )
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", str(APP_DIR)], capture_output=True)
        print("  Registered with Launch Services")
    else:
        print("  lsregister not found (app will still work)")


def main():
    print(f"Building {APP_NAME}.app ...")
    print(f"  Project: {PROJECT_DIR}")
    print(f"  Python:  {PYTHON}")
    print(f"  Target:  {APP_DIR}")

    # Check Python exists
    if not os.path.exists(PYTHON):
        print(f"\nError: Python not found at {PYTHON}")
        print("Install with: brew install python")
        sys.exit(1)

    # Remove old bundle if exists
    if APP_DIR.exists():
        print(f"\n  Removing old bundle...")
        shutil.rmtree(APP_DIR)

    # Create directory structure
    contents = APP_DIR / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"

    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # Build components
    create_info_plist(contents)
    create_launcher(macos)
    create_icon(resources)

    # Register
    register_app()

    print(f"\n  Done! Open from Finder or run:")
    print(f"  open '/Applications/{APP_NAME}.app'")
    print(f"\n  To pin to Dock: right-click the icon > Options > Keep in Dock")


if __name__ == "__main__":
    main()
