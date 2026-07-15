#!/usr/bin/env python3
"""
Ivy CLI — Interactive command-line interface for running jobs.
Usage:
  ivy list              List all available jobs
  ivy run <job_name>    Run a job immediately
  ivy help              Show help
"""

import sys
import argparse
from job_runner import job_runner

def list_jobs():
    """Display all available jobs."""
    jobs = job_runner.list_jobs()
    print("\n📋 Available Ivy Jobs:\n")
    for job in jobs:
        print(f"  {job['display_name']}")
        print(f"    Description: {job['description']}")
        print(f"    Aliases: {job['aliases']}")
        print(f"    Schedule: {job['schedule']}")
        print()

def run_job(job_name):
    """Execute a job by name."""
    status, message = job_runner.run_job(job_name)
    print(f"\n{message}\n")
    return status.name == "SUCCESS"

def main():
    parser = argparse.ArgumentParser(
        description="Ivy CLI — Run jobs on-demand",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ivy list                  List all jobs
  ivy run sharp_picks       Run sports picks job
  ivy run happy hour        Run happy hour scout
  ivy run meals             Run weekly meal planner
  ivy run brain             Run knowledge query agent
        """
    )
    parser.add_argument("command", nargs="?", default="help", help="Command: list, run, or help")
    parser.add_argument("job_name", nargs="*", help="Job name or alias (for 'run' command)")

    args = parser.parse_args()

    if args.command == "list":
        list_jobs()
        return 0
    elif args.command == "run":
        if not args.job_name:
            print("❌ Please specify a job name. Use 'ivy list' to see available jobs.")
            return 1
        job_query = " ".join(args.job_name)
        success = run_job(job_query)
        return 0 if success else 1
    elif args.command in ["help", "-h", "--help"]:
        parser.print_help()
        return 0
    else:
        # Treat unknown command as job name
        job_query = " ".join([args.command] + args.job_name)
        success = run_job(job_query)
        return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
