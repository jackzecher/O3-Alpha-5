"""Actual Qt interactions on the same large-content fixture for each release."""
from pathlib import Path
import argparse, json, shutil, tempfile
from continuity_support import ROOT, runtime, write_report
from acceptance_ui import exercise_existing

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='o3-large-ui-') as temporary:
        base = Path(temporary); runtime(args.package.resolve(), base)
        target = base/'state'; shutil.copytree(args.state.resolve(), target)
        args.output.mkdir(parents=True, exist_ok=True)
        result = exercise_existing(target, args.output, broad=True)
        write_report(args.output/'result.json', result)
        print(json.dumps(result, indent=2))
