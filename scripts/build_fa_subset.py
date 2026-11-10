"""Rebuild the Font Awesome subset in static/vendor/fontawesome/.

The site ships only the icons it uses. When you add a new <i class="fa-solid fa-...">
icon, it renders blank until you regenerate the subset:

  1. Download Font Awesome Free 6.5.0 (css/all.css and webfonts/fa-solid-900.woff2,
     fa-regular-400.woff2) from https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/
     into one folder.
  2. List every icon used:
       grep -rhoE "fa-[a-z0-9-]+" templates static/js blueprints | sort -u
  3. pip install fonttools brotli, then run:
       python scripts/build_fa_subset.py <download-folder> static/vendor/fontawesome "fa-house fa-lock ..."
"""
import re, sys, subprocess, os
dl, out, icons = sys.argv[1], sys.argv[2], sys.argv[3].split()
css = open(os.path.join(dl, "all.css")).read()
rules = []
cps = []
for name in sorted(set(icons)):
    m = re.search(r'(?:^|\n)((?:\.[a-z0-9-]+::before,\s*)*)\.' + re.escape(name) + r'::before\s*\{\s*content:\s*"\\([0-9a-f]+)"', css)
    if not m:
        m = re.search(r'\.' + re.escape(name) + r'::before,[^{]*\{\s*content:\s*"\\([0-9a-f]+)"', css)
        cp = m.group(1)
    else:
        cp = m.group(2)
    cps.append(cp)
    rules.append(f'.{name}::before{{content:"\\{cp}"}}')
os.makedirs(out, exist_ok=True)
uni = ",".join("U+" + c for c in sorted(set(cps)))
for src, dst in (("fa-solid-900.woff2", "fa-solid-900.subset.woff2"), ("fa-regular-400.woff2", "fa-regular-400.subset.woff2")):
    subprocess.check_call([sys.executable, "-m", "fontTools.subset", os.path.join(dl, src), f"--unicodes={uni}", "--flavor=woff2", f"--output-file={os.path.join(out, dst)}", "--no-hinting", "--desubroutinize", "--layout-features=", "--name-IDs=*", "--notdef-outline"])
head = """/*!
 * Font Awesome Free 6.5.0 by @fontawesome - https://fontawesome.com
 * License - https://fontawesome.com/license/free (Icons: CC BY 4.0, Fonts: SIL OFL 1.1, Code: MIT License)
 * Copyright 2023 Fonticons, Inc.
 * Subset for ACCEM: only the icons used by this site. Regenerate when adding an icon
 * (codepoints from the official css/all.css, glyphs subset with fontTools).
 */
@font-face{font-family:"Font Awesome 6 Free";font-style:normal;font-weight:900;font-display:block;src:url("fa-solid-900.subset.woff2?v=6.5.0-s1") format("woff2")}
@font-face{font-family:"Font Awesome 6 Free";font-style:normal;font-weight:400;font-display:block;src:url("fa-regular-400.subset.woff2?v=6.5.0-s1") format("woff2")}
.fa,.fa-classic,.fas,.fa-solid,.far,.fa-regular{-moz-osx-font-smoothing:grayscale;-webkit-font-smoothing:antialiased;display:var(--fa-display,inline-block);font-style:normal;font-variant:normal;line-height:1;text-rendering:auto;font-family:"Font Awesome 6 Free"}
.fa,.fas,.fa-solid{font-weight:900}
.far,.fa-regular{font-weight:400}
.fa-fw{text-align:center;width:1.25em}
.sr-only-fa,.fa-sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border-width:0}
"""
open(os.path.join(out, "fa-subset.css"), "w").write(head + "\n".join(rules) + "\n")
print(len(rules), "icons:", uni)
