"""One bounded process for explicitly approved, trusted private code."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sys


def main():
    payload=json.load(sys.stdin);name,function=payload['entrypoint'].split(':')
    folder=Path(payload['folder']).resolve();path=(folder/name).resolve()
    if not path.is_relative_to(folder):raise ValueError('Entrypoint escaped bundle')
    spec=importlib.util.spec_from_file_location('private_harness_extension',path)
    module=importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
        result=getattr(module,function)(payload['context'])
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
