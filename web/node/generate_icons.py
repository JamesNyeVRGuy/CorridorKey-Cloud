"""Generate .ico and .png icons from the CorridorKey diamond mark SVG.

Run once to create the icon files:
    python web/node/generate_icons.py

Outputs:
    web/node/icon.ico  (Windows, multi-resolution: 16, 32, 48, 256)
    web/node/icon.png  (256x256, for Linux .desktop and tray fallback)
"""

from PIL import Image, ImageDraw


def create_icon(size: int) -> Image.Image:
    """Render the Corridor Digital diamond mark at the given size.

    Recreates the SVG geometry as PIL polygon draws — no SVG library needed.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Scale factor from 740x742 viewBox to target size
    s = size / 740

    yellow = (248, 239, 37, 255)

    # Outer diamond petals (from favicon.svg polygons)
    polys = [
        # Top-left
        [(0, 345.7), (345.7, 0), (351.32, 112.85), (112.34, 351.83)],
        # Top-right
        [(393.98, 0.26), (739.68, 345.96), (626.83, 351.57), (387.85, 112.6)],
        # Bottom-right
        [(739.42, 396.11), (393.72, 741.81), (388.11, 628.96), (627.08, 389.98)],
        # Inner top-left
        [(354.72, 168.68), (169.53, 353.87), (280.17, 359.66), (359.15, 280.68)],
        # Inner bottom-left
        [(168.85, 387.32), (354.04, 572.51), (359.83, 461.87), (280.85, 382.89)],
        # Inner bottom-right
        [(385.04, 573.19), (570.23, 388), (459.6, 382.21), (380.62, 461.19)],
    ]

    for poly in polys:
        scaled = [(x * s, y * s) for x, y in poly]
        draw.polygon(scaled, fill=yellow)

    return img


def main():
    import os

    out_dir = os.path.dirname(os.path.abspath(__file__))

    # Generate multi-resolution .ico for Windows
    sizes = [16, 32, 48, 256]
    images = [create_icon(s) for s in sizes]
    ico_path = os.path.join(out_dir, "icon.ico")
    images[0].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:])
    print(f"Created {ico_path}")

    # Generate 256x256 PNG for Linux and tray fallback
    png_path = os.path.join(out_dir, "icon.png")
    images[-1].save(png_path, format="PNG")
    print(f"Created {png_path}")


if __name__ == "__main__":
    main()
