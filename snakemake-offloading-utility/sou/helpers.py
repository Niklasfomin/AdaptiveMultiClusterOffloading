import json
import logging
import math
import os
import re
import subprocess
import sys
from ftplib import FTP, error_perm
from pathlib import Path
from urllib.parse import splitport

import yaml

logger = logging.getLogger("sou")
snakemake_params = ""


def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_config():
    config_path = Path("sou-config.yaml")

    if config_path.exists():
        with config_path.open("r") as file:
            data = yaml.safe_load(file)
        return data
    else:
        logger.warning(f"File {config_path} does not exist")
        return


def run_command(command_line: str, live: bool) -> str | int:
    if live:
        stdout = sys.stdout
        stderr = sys.stderr
    else:
        stdout = stderr = subprocess.PIPE
    try:
        result = subprocess.run(
            command_line,
            shell=True,
            check=True,
            stdout=stdout,
            stderr=stderr,
            text=True,
            executable="/bin/bash",
        )
        if live:
            return result.returncode
        else:
            return result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Command failed with error: {e}") from e


def set_snakemake_params(params: str):
    global snakemake_params
    snakemake_params = params


def get_snakemake_params() -> str:
    global snakemake_params
    return snakemake_params


def remove_storage_prefix(path: str) -> str:
    if path is None:
        return path

    path = str(path).strip()
    snakemake_pattern = r"^\.snakemake/storage/[^/]+/"
    protocol_pattern = r"^\w+://"
    local_pattern = r"workflow/"

    if re.match(snakemake_pattern, path):
        return re.sub(snakemake_pattern, "", path)
    if re.match(protocol_pattern, path):
        return re.sub(protocol_pattern, "", path)
    if re.search(local_pattern, path):
        return path.split("workflow/", 1)[1]

    # Plain local relative/absolute paths are valid; keep unchanged.
    return path


def get_local_file_sizes(paths: list[str]) -> dict[str, float]:
    directory_size = 0.004096  # MB, mirrors benchmark metadata convention
    file_sizes = {}
    for path in paths:
        path_obj = Path(path)
        if path_obj.exists():
            if path_obj.is_dir():
                file_sizes[str(path)] = directory_size
            else:
                file_sizes[str(path)] = path_obj.stat().st_size / (10**6)
        else:
            logger.warning(
                f"Could not retrieve size of local file '{path}', assuming it's a directory"
            )
            file_sizes[str(path)] = directory_size
    return file_sizes


def get_initial_input_file_sizes(paths: list[str]) -> dict[str, float]:
    if not paths:
        return {}
    if all(re.match(r"^\w+://", path) for path in paths):
        schemes = {path.split("://", 1)[0] for path in paths}
        if schemes == {"ftp"}:
            return get_remote_file_sizes_ftp(
                [path.split("://", 1)[1] for path in paths]
            )
        raise Exception(
            f"SOU does not support automatic input size retrieval for schemes: {sorted(schemes)}"
        )
    if any(re.match(r"^\w+://", path) for path in paths):
        raise Exception("Cannot mix local/NFS paths and URL storage paths")
    return get_local_file_sizes(paths)


def get_remote_file_sizes_ftp(paths: list[str]) -> dict[str, int]:
    username = os.getenv("SNAKEMAKE_STORAGE_FTP_USERNAME") or os.getenv(
        "SNAKEMAKE_STORAGE_FTP_LESHL_USERNAME"
    )
    password = os.getenv("SNAKEMAKE_STORAGE_FTP_PASSWORD") or os.getenv(
        "SNAKEMAKE_STORAGE_FTP_LESHL_PASSWORD"
    )
    if not username or not password:
        raise Exception(
            "Please provide SOU with FTP username and password by setting environment variables "
            "SNAKEMAKE_STORAGE_FTP_USERNAME and SNAKEMAKE_STORAGE_FTP_PASSWORD.\n"
            "SOU currently only supports automatic retrieval of input file sizes via FTP.\n"
            "If you use a different Snakemake storage plugin, an alternative must be implemented."
        )
    username = username.strip("\"'")
    password = password.strip("\"'")

    # check if all paths have the same ftp host
    if len(set([p.split("/")[0] for p in paths])) > 1:
        raise Exception("All paths must have the same FTP host")

    ftp_netloc = paths[0].split("/")[0]
    ftp_host, ftp_port = splitport(ftp_netloc)
    ftp_port = int(ftp_port) if ftp_port else 21
    paths = ["/" + "/".join(p.split("/")[1:]) for p in paths]

    directory_size = 0.004096  # default for directories (only metadata); benchmarks report the same size
    file_sizes = {}
    with FTP() as ftp:
        ftp.connect(ftp_host, ftp_port)
        ftp.login(user=username, passwd=password)
        ftp.voidcmd("TYPE I")
        for path in paths:
            try:
                size = ftp.size(path)
                if size is not None:
                    file_sizes[path] = size / (10**6)
                else:
                    logger.info(
                        f"File size for '{path}' is None, assuming it's a directory"
                    )
                    file_sizes[path] = directory_size
            except error_perm as e:
                if str(e).startswith("550"):
                    # Directory or non-existent
                    logger.warning(
                        f"Could not retrieve size of file '{path}': {e}. Assuming it's a directory"
                    )
                    file_sizes[path] = directory_size
                else:
                    raise

    # recover FTP host in paths
    file_sizes = {f"{ftp_netloc}{path}": size for path, size in file_sizes.items()}
    return file_sizes


def log_predictions(
    runs_dir: str,
    offloading_strategy: str,
    runtime: float,
    offloaded_jobs: dict,
    cost: float,
    profiling_environment: str | None = None,
):
    path = Path(".sou/latest_workflow_predictions.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        with path.open("r+", encoding="utf-8") as f:
            try:
                predictions = json.load(f)
            except json.JSONDecodeError:
                predictions = []
            f.seek(0)
    else:
        predictions = []
        with path.open("w", encoding="utf-8") as f:
            json.dump(predictions, f)

    entry = {
        "runs_directory": str(runs_dir),
        "offloading_strategy": offloading_strategy,
        "profiling_environment": profiling_environment,
        "prediction": runtime,
        "offloaded_jobs": offloaded_jobs,
        "cost": cost,
    }
    predictions.append(entry)

    with path.open("w", encoding="utf-8") as f:
        json.dump(predictions, f, indent=4)


def log_approaching_deadline(
    latest_runtime_estimate: float, no_offloaded_jobs: int, offloading_strategy: str
):
    path = Path(f".sou/steps_until_deadline_{offloading_strategy.lower()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        with path.open("r+", encoding="utf-8") as f:
            try:
                predictions = json.load(f)
            except json.JSONDecodeError:
                predictions = {}
            f.seek(0)
    else:
        predictions = {}
        with path.open("w", encoding="utf-8") as f:
            json.dump(predictions, f)

    predictions[no_offloaded_jobs] = math.ceil(latest_runtime_estimate)

    with path.open("w", encoding="utf-8") as f:
        json.dump(predictions, f, indent=4)
