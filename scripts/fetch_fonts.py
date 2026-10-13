"""
scripts/fetch_fonts.py  —  run at image build time (see Dockerfile)
====================================================================
Downloads the poster title fonts (SIL Open Font License, from github.com/google/fonts) and writes one static .ttf per
font code into the target folder (default /app/fonts). Variable fonts are pinned to the weight the website uses
(fontTools ships with matplotlib), because the image renderer cannot pick a weight from a variable font.

    python scripts/fetch_fonts.py /app/fonts

A font that cannot be downloaded is skipped: the poster image then falls back to a bold serif.
The codes match lib/poster.ts (frontend) and backdrop_service.FONTS.
"""
import io
import os
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
FONTS = {   # code: (path in google/fonts, axis values for variable fonts or None)
    "playfair": ("playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf", {"wght": 900}),
    "dm_serif": ("dmserifdisplay/DMSerifDisplay-Regular.ttf", None),
    "abril":    ("abrilfatface/AbrilFatface-Regular.ttf", None),
    "bodoni":   ("bodonimoda/BodoniModa%5Bopsz,wght%5D.ttf", {"wght": 800, "opsz": 96}),
    "fraunces": ("fraunces/Fraunces%5BSOFT,WONK,opsz,wght%5D.ttf", {"wght": 800, "opsz": 144, "SOFT": 0, "WONK": 0}),
    "oswald":   ("oswald/Oswald%5Bwght%5D.ttf", {"wght": 700}),
    "bebas":    ("bebasneue/BebasNeue-Regular.ttf", None),
    "anton":    ("anton/Anton-Regular.ttf", None),
}


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for code, (path, axes) in FONTS.items():
        target = os.path.join(out_dir, f"{code}.ttf")
        if os.path.exists(target):
            continue
        try:
            raw = urllib.request.urlopen(BASE + path, timeout=60).read()
            if axes:
                from fontTools.ttLib import TTFont
                from fontTools.varLib.instancer import instantiateVariableFont
                font = TTFont(io.BytesIO(raw))
                have = {a.axisTag for a in font["fvar"].axes}
                static = instantiateVariableFont(font, {k: v for k, v in axes.items() if k in have}, updateFontNames=False)
                static.save(target)
            else:
                with open(target, "wb") as f:
                    f.write(raw)
            print(f"[fonts] {code}: ok")
        except Exception as exc:          # the renderer falls back to a bold serif
            print(f"[fonts] {code}: skipped ({exc})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/app/fonts")