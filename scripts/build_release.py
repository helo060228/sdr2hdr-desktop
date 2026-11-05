"""Run integration tests and build a Windows release, without publishing it."""
from pathlib import Path
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def version():
    module = ast.parse((ROOT/'source/hdr_core.py').read_text(encoding='utf-8'))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError('VERSION is missing')


def run(arguments):
    subprocess.run(arguments, cwd=ROOT, check=True)


def main():
    if os.name != 'nt':
        raise SystemExit('Build the Windows executable on Windows.')
    release_version = version()
    run([sys.executable, '-m', 'unittest', 'discover', '-s', 'source', '-p', 'test_conversion.py', '-v'])
    build = ROOT/'build'
    build.mkdir(exist_ok=True)
    stage = ROOT/'dist'/f'SDR2HDR-v{release_version}-Windows'
    stage.mkdir(parents=True, exist_ok=True)
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
         '--name', 'SDR2HDR', '--distpath', str(stage), '--workpath', str(build/'pyinstaller'),
         '--specpath', str(build), '--paths', str(ROOT/'source'), str(ROOT/'source/app.py')])
    for name in ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md', 'requirements-build.txt']:
        shutil.copy2(ROOT/name, stage/name)
    for name in ['source', 'docs', 'third_party', 'assets', 'scripts']:
        shutil.copytree(ROOT/name, stage/name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
    exe_hash = hashlib.sha256((stage/'SDR2HDR.exe').read_bytes()).hexdigest()
    (stage/'build-info.json').write_text(json.dumps({
        'version': release_version, 'platform': 'Windows x64',
        'python': sys.version.split()[0], 'ffmpeg_bundled': False,
        'integration_tests': 'passed', 'exe_sha256': exe_hash,
    }, indent=2), encoding='utf-8')
    (stage/'SHA256SUMS.txt').write_text(f'{exe_hash}  SDR2HDR.exe\n', encoding='ascii')
    archive = ROOT/'dist'/f'SDR2HDR-Windows-v{release_version}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                package.write(path, path.relative_to(stage.parent))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(f'Built: {archive.name}')


if __name__ == '__main__':
    main()
