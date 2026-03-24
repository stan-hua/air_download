"""Command-line interface for air_download."""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from air_download.client import AIRClient

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Command line interface to the Automated Image Retrieval (AIR) "
            "Portal."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "acc",
        nargs="?",
        metavar="ACCESSION",
        help="Accession number to search or download.",
    )
    parser.add_argument(
        "--url",
        help=(
            "AIR API URL (e.g. https://air.<domain>.edu/api/). If not "
            "provided, resolved from AIR_URL in the credential file or "
            "the AIR_URL environment variable."
        ),
        default=None,
    )
    parser.add_argument(
        "-c",
        "--cred-path",
        help=(
            "Login credentials file (dotenv format with AIR_USERNAME, "
            "AIR_PASSWORD, and optionally AIR_URL). If not provided, "
            "credentials are read from environment variables."
        ),
        default=None,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output path or directory.",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "-pf",
        "--profile",
        help="Anonymization profile ID.",
        default=-1,
    )
    parser.add_argument(
        "-pj",
        "--project",
        help="Project ID.",
        default=-1,
    )
    parser.add_argument(
        "-lpj",
        "--list-projects",
        action="store_true",
        help="List available project IDs.",
    )
    parser.add_argument(
        "-lpf",
        "--list-profiles",
        action="store_true",
        help="List available anonymization profiles.",
    )
    parser.add_argument(
        "-mrn",
        "--mrn",
        help="Patient MRN (Medical Record Number) to search/download exams for.",
    )
    parser.add_argument(
        "-xm",
        "--exam_modality_inclusion",
        help=(
            "Comma-separated list of exam modality inclusion patterns "
            "(case-insensitive, OR logic). Example: 'MR,CT'"
        ),
        default=None,
    )
    parser.add_argument(
        "-xd",
        "--exam_description_inclusion",
        help=(
            "Comma-separated list of exam description inclusion patterns "
            "(case-insensitive, OR logic). Example: 'BRAIN WITH AND WITHOUT "
            "CONTRAST'"
        ),
        default=None,
    )
    parser.add_argument(
        "-xm-exclude",
        "--exam_modality_exclusion",
        help=(
            "Comma-separated list of exam modality exclusion patterns "
            "(case-insensitive, OR logic). Excludes matching exams."
        ),
        default=None,
    )
    parser.add_argument(
        "-xd-exclude",
        "--exam_description_exclusion",
        help=(
            "Comma-separated list of exam description exclusion patterns "
            "(case-insensitive, OR logic). Excludes matching exams."
        ),
        default=None,
    )
    parser.add_argument(
        "-s",
        "--series_inclusion",
        help=(
            "Comma-separated list of series inclusion patterns "
            "(case-insensitive, OR logic). Example for T1 type series: "
            "'t1,spgr,bravo,mpr'"
        ),
        default=None,
    )
    parser.add_argument(
        "-s-exclude",
        "--series_exclusion",
        help=(
            "Comma-separated list of series exclusion patterns "
            "(case-insensitive, OR logic). Excludes matching series."
        ),
        default=None,
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help=(
            "Only search for exams matching the provided parameters without "
            "downloading. Works with both ACCESSION and --mrn. "
            "Prints a summary table to stdout. "
            "If -o is also provided, writes results to <output>/accessions.csv."
        ),
    )
    # Hidden backward-compatibility alias
    parser.add_argument(
        "--only-return-accessions",
        action="store_true",
        dest="search_only",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG level) logging.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output except errors.",
    )

    arguments = parser.parse_args()

    if not (arguments.list_projects or arguments.list_profiles):
        if not arguments.acc and not arguments.mrn:
            parser.error("Must specify either ACCESSION or --mrn.")

    return arguments


def _print_exams_table(exams: list[dict[str, Any]]) -> None:
    """Print exam search results as a formatted table to stdout.

    Args:
        exams: List of exam dictionaries from the API.
    """
    col_widths = {"mrn": 12, "accession": 14, "date": 12, "mod": 5, "description": 40}
    header = (
        f"{'MRN':<{col_widths['mrn']}}"
        f"{'Accession':<{col_widths['accession']}}"
        f"{'Date':<{col_widths['date']}}"
        f"{'Mod':<{col_widths['mod']}}"
        f"{'Description':<{col_widths['description']}}"
        f"  Images"
    )
    separator = "-" * len(header)
    print(header)
    print(separator)
    for exam in exams:
        date = (exam.get("dateTime") or "")[:10]
        mrn = exam.get("patientId", "")
        print(
            f"{mrn:<{col_widths['mrn']}}"
            f"{exam.get('accessionNumber', ''):<{col_widths['accession']}}"
            f"{date:<{col_widths['date']}}"
            f"{exam.get('modality', ''):<{col_widths['mod']}}"
            f"{(exam.get('description') or ''):<{col_widths['description']}}"
            f"  {exam.get('imageCount', '')}"
        )


def _configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure logging based on verbosity flags.

    Only the ``air_download`` logger is affected. Third-party loggers
    (e.g. ``urllib3``, ``requests``) stay at WARNING to avoid noisy output.

    Args:
        verbose: If True, set log level to DEBUG.
        quiet: If True, set log level to ERROR.
    """
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    pkg_logger = logging.getLogger("air_download")
    pkg_logger.setLevel(level)
    pkg_logger.addHandler(handler)


def main(args: argparse.Namespace) -> None:
    """Execute the main application logic based on parsed arguments.

    Args:
        args: Parsed command-line arguments.
    """
    client = AIRClient(url=args.url, cred_path=args.cred_path)

    if args.list_projects or args.list_profiles:
        if args.list_projects:
            projects = client.list_projects()
            print("Available projects:")
            for project in projects:
                print(f"  ID: {project['id']}, Name: {project['name']}")
        if args.list_profiles:
            if args.list_projects:
                print()
            profiles = client.list_profiles()
            print("Available anonymization profiles:")
            for profile in profiles:
                print(
                    f"  ID: {profile['id']}, Name: {profile['name']}, "
                    f"Description: {profile['description']}"
                )
        return

    exams = client.download(
        accession=args.acc,
        mrn=args.mrn,
        output=args.output,
        project=args.project,
        profile=args.profile,
        exam_modality_inclusion=args.exam_modality_inclusion,
        exam_description_inclusion=args.exam_description_inclusion,
        exam_modality_exclusion=args.exam_modality_exclusion,
        exam_description_exclusion=args.exam_description_exclusion,
        series_inclusion=args.series_inclusion,
        series_exclusion=args.series_exclusion,
        search_only=args.search_only,
    )

    if args.search_only and exams:
        if args.output is None:
            _print_exams_table(exams)
        else:
            logger.info("Found %d exam(s). Results written to %s.", len(exams), args.output / "accessions.csv")


def cli() -> None:
    """CLI entry point."""
    args = parse_args()
    _configure_logging(verbose=args.verbose, quiet=args.quiet)
    main(args)


if __name__ == "__main__":
    cli()
