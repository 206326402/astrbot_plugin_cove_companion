#!/usr/bin/env python3
"""Create a market-safe validation archive, excluding runtime data and credentials."""
from pathlib import Path
import shutil, zipfile
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'dist'/'astrbot_plugin_cove_companion-1.4.1.zip'
OUT.parent.mkdir(exist_ok=True)
if OUT.exists(): OUT.unlink()
blocked=('node_modules','__pycache__','.git','dist','data')
with zipfile.ZipFile(OUT,'w',zipfile.ZIP_DEFLATED) as z:
    for p in ROOT.rglob('*'):
        if not p.is_file(): continue
        rel=p.relative_to(ROOT).as_posix()
        if any(part in blocked for part in p.relative_to(ROOT).parts): continue
        if rel.startswith('dist/') or rel.endswith(('.sqlite3','.sqlite3-wal','.sqlite3-shm','.log')) or Path(rel).name.startswith('.env'): continue
        z.write(p,rel)
print(OUT)
print(f'{OUT.stat().st_size} bytes')
if OUT.stat().st_size >= 16*1024*1024: raise SystemExit('archive exceeds 16 MiB')
