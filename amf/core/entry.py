"""Console-script entry point for the ``amf-pipeline`` command.

pyproject.toml declares ``amf-pipeline = "amf.core.entry:main"``. The published
wheel and the Docker image both depend on that target resolving, so this module
has to exist for the artifact to be usable at all.
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata


def package_version() -> str:
    """Return the installed distribution version, or a marker when running from source."""
    try:
        return metadata.version("amf")
    except metadata.PackageNotFoundError:
        return "0.0.0+source"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amf-pipeline",
        description="AMF pipeline runner.",
    )
    parser.add_argument("--version", action="version", version=f"amf {package_version()}")
    parser.add_argument("--wave", help="Wave the job belongs to")
    parser.add_argument("--job", help="Job to run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.job:
        build_parser().print_help()
        return 0

    print(f"amf {package_version()}: wave={args.wave} job={args.job}")
    print("No ingestion runtime is wired up yet; this is the POC packaging entry point.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
