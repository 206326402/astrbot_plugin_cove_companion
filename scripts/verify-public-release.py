#!/usr/bin/env python3
"""Check that the public tree is safe and recognizable as an AstrBot plugin."""
from __future__ import annotations
import json, re, sys, zipfile
from pathlib import Path
try:
    from PIL import Image
except Exception:
    Image = None
ROOT = Path(__file__).resolve().parents[1]
required = ["metadata.yaml", "main.py", "_conf_schema.json", "README.md", "LICENSE", "NOTICE.md", "logo.png", "assets/market-cover.png", "assets/screenshots/player.png", "assets/screenshots/listening-journal.png", "service/package.json", "scripts/install-cove.sh", "scripts/check-cove.sh"]
errors=[]
for name in required:
    if not (ROOT/name).is_file(): errors.append(f"missing {name}")
meta=(ROOT/'metadata.yaml').read_text(encoding='utf-8-sig')
def field(name):
    m=re.search(rf'(?m)^{re.escape(name)}:\s*["\']?([^"\'\r\n]+)',meta)
    return m.group(1).strip() if m else ''
if field('name')!='astrbot_plugin_cove_companion': errors.append('metadata name mismatch')
if field('version')!='1.4.0': errors.append('metadata version must be 1.4.0')
if field('repo')!='https://github.com/206326402/astrbot_plugin_cove_companion': errors.append('metadata repo mismatch')
if field('logo')!='logo.png': errors.append('metadata logo must reference root logo.png')
try: json.loads((ROOT/'_conf_schema.json').read_text(encoding='utf-8-sig'))
except Exception as exc: errors.append(f'config schema invalid: {exc}')
if Image:
    try:
        with Image.open(ROOT/'assets/market-cover.png') as im:
            if im.size != (1024,1024): errors.append(f'cover size is {im.size}, expected 1024x1024')
    except Exception as exc: errors.append(f'cover unreadable: {exc}')
    try:
        with Image.open(ROOT/'logo.png') as im:
            if im.size != (256,256): errors.append(f'logo size is {im.size}, expected 256x256')
    except Exception as exc: errors.append(f'logo unreadable: {exc}')
for p in ROOT.rglob('*'):
    if not p.is_file(): continue
    rel=p.relative_to(ROOT).as_posix().lower()
    if any(x in rel for x in ('node_modules/','service/dist/','service/data/','.git/','__pycache__/')): continue
    if p.suffix in {'.sqlite3','.db'} or rel.endswith(('.env','.log')): errors.append(f'forbidden runtime file: {rel}')
    text=''
    if p.suffix in {'.py','.js','.ts','.json','.yaml','.yml','.md','.sh','.service'} and p.stat().st_size < 2_000_000:
        try: text=p.read_text(encoding='utf-8',errors='ignore')
        except Exception: pass
    if re.search(r'github_pat_[A-Za-z0-9_]+|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY',text,re.I): errors.append(f'possible credential pattern: {rel}')
if errors:
    print('\n'.join('ERROR: '+e for e in errors)); sys.exit(1)
print('public release checks passed')
print('package candidate files:', sum(1 for p in ROOT.rglob('*') if p.is_file()))

