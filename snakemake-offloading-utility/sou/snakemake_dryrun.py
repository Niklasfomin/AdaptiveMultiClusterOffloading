import logging
import re
from pathlib import Path

from sou.helpers import get_snakemake_params, run_command, remove_storage_prefix

DRYRUN_CACHE = ".sou/dryrun.txt"

logger = logging.getLogger("sou")


def parse_file_list(line: str) -> list[str]:
    value = line.split(":", 1)[1].strip()
    if not value:
        return []
    files = []
    for raw_file in value.split(","):
        file = re.sub(r"\s*\([^()]*\)\s*$", "", raw_file).strip()
        if file:
            files.append(remove_storage_prefix(file))
    return files


class SnakemakeDryrun:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized") or not Path(DRYRUN_CACHE).exists():
            self.dryrun_text = self._get_dryrun_from_snakemake()
            self.requested_resources, self.job_files = self._parse_dryrun()
            self.initialized = True

    def _get_dryrun_from_snakemake(self):
        if Path(DRYRUN_CACHE).exists():
            logger.info(f"Reading cached dryrun from '.sou/dryrun.txt'")
            with open(Path(".sou/dryrun.txt")) as f:
                return f.read()
        else:
            logger.info(f"Executing snakemake dryrun...")
            command = f"snakemake {get_snakemake_params()} --dryrun"
            result = run_command(command, live=False)
            Path(DRYRUN_CACHE).parent.mkdir(parents=True, exist_ok=True)
            with open(Path(DRYRUN_CACHE), "w") as f:
                f.write(result)
        return result

    def _parse_dryrun(self):
        jobids_to_resources = {}
        jobids_to_files = {}
        current_jobid = None
        current_files = {}

        for line in self.dryrun_text.splitlines():
            line = line.strip()

            if line.startswith("input:"):
                current_files["inputs"] = parse_file_list(line)

            elif line.startswith("output:"):
                current_files["outputs"] = parse_file_list(line)

            elif line.startswith("log:"):
                current_files["logs"] = parse_file_list(line)

            elif line.startswith("jobid:"):
                current_jobid = int(line.split(":", 1)[1].strip())
                jobids_to_resources[current_jobid] = {}
                jobids_to_files[current_jobid] = current_files
                current_files = {}

            elif line.startswith("threads:"):
                if "<TBD>" in line:
                    jobids_to_resources[current_jobid]["threads"] = 1
                else:
                    jobids_to_resources[current_jobid]["threads"] = int(
                        line.split(":")[1].strip()
                    )

            elif line.startswith("resources:"):
                mem_match = re.search(r"mem_mb=(\d+),", line)
                if mem_match:
                    mem_mb = int(mem_match.group(1))
                    jobids_to_resources[current_jobid]["mem_mb"] = mem_mb

                disk_match = re.search(r"disk_mb=(\d+),", line)
                if disk_match:
                    disk_mb = int(disk_match.group(1))
                    jobids_to_resources[current_jobid]["disk_mb"] = disk_mb

        logger.info(f"Parsed {len(jobids_to_resources)} job entries from dryrun output")
        return jobids_to_resources, jobids_to_files

    def get_requested_resources(self, jobid: int) -> dict:
        try:
            return self.requested_resources[jobid]
        except KeyError:
            logger.warning(f"Requested resources for job {jobid} not found in dryrun")
            return {}

    def get_job_files(self):
        return self.job_files
