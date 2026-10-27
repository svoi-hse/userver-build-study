#!/usr/bin/env python3
"""Reproduce the debug-compression incompatibility independently of userver."""
import pathlib
import shlex
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix='debug-compression-') as temporary:
    directory = pathlib.Path(temporary)
    source = directory / 'main.cpp'
    source.write_text('int main(int argc, char**) { return argc == 42; }\n')
    with pathlib.Path('/results/compression-probe.txt').open('w') as report:
        for mode in ('zstd', 'zlib'):
            report.write(f'\nDebug compression: {mode}\n')
            obj = directory / f'{mode}.o'
            commands = [
                ['clang++-18', '-g', f'-gz={mode}', '-c', str(source), '-o', str(obj)],
                ['readelf', '-x', '.debug_info', str(obj)],
                ['clang++-18', '-Wl,--compress-debug-sections=zlib', str(obj), '-o', str(directory / mode)],
            ]
            for command in commands:
                report.write('$ ' + shlex.join(command) + '\n')
                report.flush()
                result = subprocess.run(command, stdout=report, stderr=subprocess.STDOUT)
                report.write(f'exit={result.returncode}\n')
