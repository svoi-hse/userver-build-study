#!/usr/bin/env python3
"""Run a controlled clean-build comparison. No benchmark values are hard-coded."""

import difflib
import hashlib
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import time

ROOT = pathlib.Path('/work')
OUT = pathlib.Path('/results')
STUDY = pathlib.Path(os.environ['STUDY'])
CBA = os.environ['CBA']
RUNS = int(os.environ.get('RUNS', '3'))
SERVICE = 'userver_study'


def run(command, log, cwd=None, check=True):
    print('+', ' '.join(map(str, command)), flush=True)
    started = time.perf_counter()
    with pathlib.Path(log).open('w') as stream:
        proc = subprocess.run(list(map(str, command)), cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    elapsed = time.perf_counter() - started
    if check and proc.returncode:
        print(pathlib.Path(log).read_text()[-12000:], flush=True)
        raise RuntimeError(f'{command[0]} failed, see {log}')
    return {'seconds': elapsed, 'exit_code': proc.returncode}


def capture(command):
    return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trace_metrics(directory):
    files = []
    for path in sorted(directory.rglob('*.json')):
        try:
            trace = json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(trace, dict) or not isinstance(trace.get('traceEvents'), list):
            continue
        totals = {event['name']: event['dur'] / 1e6 for event in trace['traceEvents']
                  if event.get('ph') == 'X' and event.get('name', '').startswith('Total ') and 'dur' in event}
        files.append({'file': str(path.relative_to(directory)), 'totals_seconds': totals})
    if not files:
        raise RuntimeError(f'No compiler traces in {directory}')
    names = sorted({key for entry in files for key in entry['totals_seconds']})
    return {
        'trace_count': len(files),
        'sum_seconds': {name: sum(entry['totals_seconds'].get(name, 0) for entry in files) for name in names},
        'files': files,
    }


def ninja_metrics(path):
    tasks = []
    for line in path.read_text().splitlines():
        if line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) == 5:
            tasks.append({'start_ms': int(fields[0]), 'end_ms': int(fields[1]), 'output': fields[3]})
    objects = [task for task in tasks if task['output'].endswith(('.o', '.pch'))]
    links = [task for task in tasks if task['output'] == SERVICE]
    return {'object_phase_seconds': max(task['end_ms'] for task in objects) / 1000,
            'executable_link_seconds': sum(task['end_ms'] - task['start_ms'] for task in links) / 1000,
            'tasks': tasks}


def main():
    environment = {
        'uname': capture(['uname', '-srm']),
        'lscpu': capture(['lscpu']),
        'memory': pathlib.Path('/proc/meminfo').read_text(),
        'clang': capture(['clang++-18', '--version']),
        'cmake': capture(['cmake', '--version']),
        'ninja': capture(['ninja', '--version']),
        'resource_limits': {},
        'method': {
            'target': SERVICE, 'build_type': 'Release', 'jobs': 4,
            'clean_build': True, 'compiler_cache_disabled': True,
            'trace_granularity_us': 500,
            'debug_compression': 'zlib, explicitly set for both variants; avoids Clang zstd / old GNU ld mismatch',
            'timed': 'cmake --build only; configure, analyzer, copying and downloading excluded',
            'filesystem': 'container Linux filesystem; results exported after each build',
            'order': 'alternating paired AB/BA; one unreported warm-up build for each variant',
        },
    }
    for name in ('cpu.max', 'cpuset.cpus.effective', 'memory.max', 'memory.swap.max'):
        path = pathlib.Path('/sys/fs/cgroup') / name
        if path.exists():
            environment['resource_limits'][name] = path.read_text().strip()
    (OUT / 'environment.json').write_text(json.dumps(environment, indent=2, ensure_ascii=False) + '\n')

    run(['userver-create-service', '--grpc', '--mongo', '--postgresql', SERVICE],
        OUT / 'generator.log', cwd=STUDY)
    original = STUDY / SERVICE
    variants = {}
    for variant in ('baseline', 'optimized'):
        destination = STUDY / variant / SERVICE
        destination.parent.mkdir()
        shutil.copytree(original, destination)
        variants[variant] = destination

    cmake = variants['optimized'] / 'CMakeLists.txt'
    before = cmake.read_text()
    after = before + '''\n# Hypothesis: reuse the expensive common userver component header across object files.\n# PCH generation is included in every clean-build measurement.\ntarget_precompile_headers(${PROJECT_NAME}_objs PRIVATE <userver/components/component.hpp>)\n'''
    cmake.write_text(after)
    (OUT / 'optimization.patch').write_text(''.join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile='baseline/CMakeLists.txt', tofile='optimized/CMakeLists.txt')))

    for variant, source in variants.items():
        shutil.copytree(source, OUT / 'sources' / variant)

    records = []
    order = [('warmup', 0, 'baseline'), ('warmup', 0, 'optimized')]
    for repetition in range(1, RUNS + 1):
        pair = ('baseline', 'optimized') if repetition % 2 else ('optimized', 'baseline')
        order.extend(('measured', repetition, variant) for variant in pair)

    for phase, repetition, variant in order:
        label = f'{phase}-{repetition}-{variant}'
        result_dir = OUT / label
        result_dir.mkdir()
        build = STUDY / f'build-{label}'
        source = variants[variant]
        configure = ['cmake', '-S', source, '-B', build, '-G', 'Ninja',
                     '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=clang++-18',
                     '-DCMAKE_C_COMPILER=clang-18', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
                     '-DCMAKE_CXX_COMPILER_LAUNCHER=', '-DCMAKE_C_COMPILER_LAUNCHER=',
                     '-DCMAKE_CXX_FLAGS=-ftime-trace -ftime-trace-granularity=500',
                     '-DUSERVER_DEBUG_INFO_COMPRESSION=z',
                     '-DUSERVER_FEATURE_TESTSUITE=OFF']
        run(configure, result_dir / 'configure.log')
        shutil.copy2(build / 'compile_commands.json', result_dir / 'compile_commands.json')
        # CMake's compiler checks can emit traces too. They are setup, not service compilation.
        for path in build.rglob('*.json'):
            try:
                data = json.loads(path.read_text())
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(data, dict) and 'traceEvents' in data:
                path.unlink()
        run([CBA, '--start', build], result_dir / 'analyzer-start.log')
        timing = run(['cmake', '--build', build, '--target', SERVICE, '-j', '4'],
                     result_dir / 'build.log', check=False)
        trace = trace_metrics(build)
        run([CBA, '--stop', build, result_dir / 'capture.bin'], result_dir / 'analyzer-stop.log')
        run([CBA, '--analyze', result_dir / 'capture.bin'], result_dir / 'analysis.txt', cwd=ROOT)
        for path in build.rglob('*.json'):
            try:
                data = json.loads(path.read_text())
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(data, dict) and 'traceEvents' in data:
                destination = result_dir / 'traces' / path.relative_to(build)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        for name in ('CMakeCache.txt', '.ninja_log'):
            if (build / name).exists():
                shutil.copy2(build / name, result_dir / name)
        binary = build / SERVICE
        row = {'label': label, 'phase': phase, 'repetition': repetition, 'variant': variant,
               'build': timing, 'traces': trace,
               'ninja': ninja_metrics(build / '.ninja_log'),
               'binary_exists': binary.exists(), 'binary_sha256': sha256(binary) if binary.exists() else None}
        if timing['exit_code']:
            log = (result_dir / 'build.log').read_text()
            row['failure_tail'] = log[-10000:]
            # A link failure is allowed by the assignment. A compile failure is not comparable.
            if 'FAILED:' not in log or not ('Linking CXX executable' in log and 'linker command failed' in log):
                (result_dir / 'metrics.json').write_text(json.dumps(row, indent=2) + '\n')
                raise RuntimeError(f'Compilation failed in {label}; refusing a partial comparison')
        if binary.exists():
            row['help_check'] = run([binary, '--help'], result_dir / 'service-help.txt', check=False)
            if row['help_check']['exit_code']:
                raise RuntimeError(f'{label}: the linked service failed the --help check')
        (result_dir / 'metrics.json').write_text(json.dumps(row, indent=2) + '\n')
        records.append(row)
        (OUT / 'measurements.json').write_text(json.dumps(records, indent=2) + '\n')
        print(f'{label}: {timing["seconds"]:.3f}s, {trace["trace_count"]} traces, exit={timing["exit_code"]}', flush=True)
        shutil.rmtree(build)

    summary = {}
    for variant in variants:
        selected = [r for r in records if r['phase'] == 'measured' and r['variant'] == variant]
        times = [r['build']['seconds'] for r in selected]
        trace_names = sorted({key for row in selected for key in row['traces']['sum_seconds']})
        summary[variant] = {
            'n': len(times), 'wall_seconds': times, 'median_wall_seconds': statistics.median(times),
            'min_wall_seconds': min(times), 'max_wall_seconds': max(times),
            'median_trace_totals_seconds': {name: statistics.median(
                row['traces']['sum_seconds'].get(name, 0) for row in selected) for name in trace_names},
            'trace_counts': [row['traces']['trace_count'] for row in selected],
            'median_object_phase_seconds': statistics.median(row['ninja']['object_phase_seconds'] for row in selected),
            'median_link_seconds': statistics.median(row['ninja']['executable_link_seconds'] for row in selected),
            'all_builds_succeeded': all(row['build']['exit_code'] == 0 for row in selected),
        }
    before_time = summary['baseline']['median_wall_seconds']
    after_time = summary['optimized']['median_wall_seconds']
    summary['comparison'] = {
        'speedup': before_time / after_time,
        'wall_reduction_percent': (before_time - after_time) / before_time * 100,
        'paired_wall_differences_seconds': [
            next(r['build']['seconds'] for r in records if r['phase'] == 'measured' and r['variant'] == 'baseline' and r['repetition'] == k)
            - next(r['build']['seconds'] for r in records if r['phase'] == 'measured' and r['variant'] == 'optimized' and r['repetition'] == k)
            for k in range(1, RUNS + 1)],
    }
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
