import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Center, HorizontalGroup, VerticalGroup
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Placeholder,
    RichLog,
    Static,
)

import sou.scheduler
from sou.helpers import (
    get_config,
    get_snakemake_params,
    run_command,
    set_snakemake_params,
)
from sou.scheduler import (
    CostModel,
    OffloadingStrategy,
    ProfilingEnvironment,
    ProfilingStrategy,
    RuntimeEstimator,
)


class RichLogHandler(logging.Handler):
    def __init__(self, log_widget: RichLog):
        super().__init__()
        self.log_widget = log_widget

    def emit(self, record):
        msg = self.format(record)
        self.log_widget.write(msg)


logger = logging.getLogger("sou")
logger.setLevel(logging.DEBUG)


class ParameterRow(HorizontalGroup):
    def __init__(self, label: str, field, **kwargs):
        super().__init__(**kwargs)
        self.label = label
        self.field = field

    def compose(self) -> ComposeResult:
        yield Label(self.label, classes="parameter_label")
        yield self.field


class Parameters(VerticalGroup):
    def compose(self) -> ComposeResult:
        yield ParameterRow(
            "Profiling strategy:",
            OptionList(
                *[pm.value for pm in ProfilingStrategy],
                id="profiling_strategy",
                classes="parameter_field option_field",
                compact=True,
            ),
        )
        yield ParameterRow(
            "Profiling environment:",
            OptionList(
                *[pe.value for pe in ProfilingEnvironment],
                id="profiling_environment",
                classes="parameter_field option_field",
                compact=True,
            ),
        )
        yield ParameterRow(
            "Runtime estimator:",
            OptionList(
                *[e.value for e in RuntimeEstimator],
                id="runtime_estimator",
                classes="parameter_field option_field",
                compact=True,
            ),
        )
        yield ParameterRow(
            "Cost model:",
            OptionList(
                *[c.value for c in CostModel],
                id="cost_model",
                classes="parameter_field option_field",
                compact=True,
            ),
        )
        yield ParameterRow(
            "Runs directory:",
            Input(
                placeholder="/path/to/runs-directory",
                id="runs_dir_input",
                classes="parameter_field",
            ),
        )
        yield ParameterRow(
            "Correlation threshold:",
            Input(
                placeholder="(default: 0.8)",
                id="threshold_input",
                classes="parameter_field",
                type="number",
                valid_empty=True,
            ),
        )
        yield ParameterRow(
            "Deadline:",
            Input(
                placeholder="(in minutes)",
                id="deadline_input",
                classes="parameter_field",
                type="number",
                valid_empty=True,
            ),
        )
        yield ParameterRow(
            "Offloading strategy:",
            OptionList(
                *[s.value for s in OffloadingStrategy],
                id="offloading_strategy_option_list",
                classes="parameter_field option_field",
                compact=True,
            ),
        )


class Buttons(HorizontalGroup):
    def compose(self) -> ComposeResult:
        yield Button(
            "Run Profiling",
            id="profiling_button",
            classes="vertical",
            variant="success",
        )
        yield Button(
            "Run Performance Estimations",
            id="predict_button",
            classes="vertical",
            variant="success",
        )
        yield Button(
            "Execute Workflow(Quit SOU)",
            id="execute_button",
            classes="vertical",
            variant="warning",
        )


class SnakemakeOffloadingUtility(App):
    TITLE = "Snakemake Offloading Utility"
    BINDINGS = [
        ("d", "toggle_dark", "Toggle dark mode"),
        ("c", "clear_cache", "Clear cache"),
    ]
    CSS_PATH = "layout.tcss"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.jobs_to_offload = None

    def compose(self) -> ComposeResult:
        self.rich_log = RichLog(id="prediction_log", markup=True)

        yield Header()
        yield Footer()
        with Center():
            yield Static("Parameters", id="sou_parameters_static", classes="heading")
        with Center():
            yield Parameters()
        with Center():
            yield Static("Actions", id="sou_actions_static", classes="heading")
        with Center():
            yield Buttons()
        with Center():
            yield Static("Logs", id="sou_logs_static", classes="heading")
        yield self.rich_log

    def action_toggle_dark(self) -> None:
        self.theme = (
            "textual-dark" if self.theme == "textual-light" else "textual-light"
        )

    def action_clear_cache(self) -> None:
        cache_path = Path(".sou")
        if cache_path.exists():
            for item in cache_path.iterdir():
                if item.is_file() and item.name in ["dryrun.txt", "d3dag.json"]:
                    item.unlink()
                    self.rich_log.write(f"[bold green]Removed {item.name}[/bold green]")
        else:
            self.rich_log.write("[bold red]Cache directory '.sou' not found[/bold red]")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        # inputs
        runs_dir = self.query_one("#runs_dir_input", Input).value
        if not runs_dir:
            self.rich_log.write(
                "[bold red]Please provide a valid path to the runs directory.[/bold red]"
            )
            return

        corr_threshold_input = self.query_one("#threshold_input", Input).value
        corr_threshold = float(corr_threshold_input) if corr_threshold_input else 0.8

        deadline = self.query_one("#deadline_input", Input).value
        deadline_seconds = float(deadline) * 60 if deadline else None

        profiling_environment_obj = self.query_one("#profiling_environment", OptionList)
        profiling_environment = ProfilingEnvironment(
            profiling_environment_obj.options[
                profiling_environment_obj.highlighted
            ].prompt
        )
        if profiling_environment in [
            ProfilingEnvironment.LOCAL,
            ProfilingEnvironment.REMOTE,
        ]:
            self.rich_log.write(
                "[bold red]The selected profiling environment is not supported for this action[/bold red]"
            )
            return

        offloading_strategy_obj = self.query_one(
            "#offloading_strategy_option_list", OptionList
        )
        offloading_strategy = OffloadingStrategy(
            offloading_strategy_obj.options[offloading_strategy_obj.highlighted].prompt
        )
        if (
            offloading_strategy
            in [
                OffloadingStrategy.LONGEST_JOB_FIRST,
                OffloadingStrategy.SMALLEST_INPUT_SIZE_FIRST,
            ]
            and not deadline
        ):
            self.rich_log.write(
                f"[bold red]The selected offloading strategy requires a deadline[/bold red]"
            )
            return

        if event.button.id in {"profiling_button", "predict_button"}:
            # logs
            handler = RichLogHandler(self.rich_log)
            handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
                )
            )
            logger.addHandler(handler)

            # predict
            await self.run_sou(
                runs_dir,
                deadline_seconds,
                offloading_strategy,
                profiling_environment,
                corr_threshold,
            )
            logger.removeHandler(handler)
        elif event.button.id == "execute_button":
            if (
                self.jobs_to_offload is None
                and offloading_strategy != OffloadingStrategy.NONE
            ):
                self.rich_log.write(
                    "[bold red]Offloading active. Please predict makespan once before executing Snakemake[/bold red]"
                )
                return
            self.rich_log.write(f"{get_snakemake_params()}")
            command = f"snakemake {get_snakemake_params()}"
            if self.jobs_to_offload:
                command += (
                    f" --offloader-jobs {','.join(map(str, self.jobs_to_offload))}"
                )
            self.exit(command)

    async def run_sou(
        self,
        runs_dir: str,
        deadline_seconds: int | None,
        offloading_strategy: OffloadingStrategy,
        profiling_environment: ProfilingEnvironment,
        corr_threshold: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(
                pool,
                sou.scheduler.run_main_safely,
                runs_dir,
                deadline_seconds,
                offloading_strategy,
                profiling_environment,
                corr_threshold,
            )
            if result:
                runtime, offloaded_jobs, cost = result
                self.jobs_to_offload = offloaded_jobs
                self.rich_log.write("")
                if runtime:
                    self.rich_log.write(
                        f"[bold green]✔ Predicted workflow makespan: {runtime / 60:.2f} minutes[/bold green]\n"
                    )
                if offloading_strategy != OffloadingStrategy.NONE:
                    if deadline_seconds:
                        if runtime > deadline_seconds:
                            self.rich_log.write(
                                f"[bold red]✘ Makespan prediction exceeds deadline of {deadline_seconds / 60:.2f} minutes[/bold red]"
                            )
                            self.rich_log.write(
                                f"[bold red]✘ Could not offload more jobs without underutilization of primary cluster[/bold red]"
                            )
                        else:
                            self.rich_log.write(
                                f"[bold green]✔ Makespan prediction meets deadline of {deadline_seconds / 60:.2f} minutes[/bold green]"
                            )
                    self.rich_log.write(
                        f"[bold green]✔ Predicted offloading costs: ${cost:.2f}[/bold green]"
                    )
                if offloaded_jobs:
                    offloaded_jobs_text = ", ".join(
                        f"{job} ({rule})" for job, rule in offloaded_jobs.items()
                    )
                    self.rich_log.write(
                        f"[bold green]✔ {len(offloaded_jobs)} Jobs selected for offloading: {offloaded_jobs_text}[/bold green]"
                    )


def parse_enum(enum_cls, value: str):
    for item in enum_cls:
        if value in {item.name, item.value}:
            return item

    if enum_cls is OffloadingStrategy:
        aliases = {
            "none": OffloadingStrategy.NONE,
            "pefo": OffloadingStrategy.PRIMARY_OCCUPIED,
            "ljf": OffloadingStrategy.LONGEST_JOB_FIRST,
            "sisf": OffloadingStrategy.SMALLEST_INPUT_SIZE_FIRST,
        }
        alias_match = aliases.get(value.lower())
        if alias_match:
            return alias_match

    choices = ", ".join(item.value for item in enum_cls)
    raise argparse.ArgumentTypeError(
        f"invalid value '{value}'. Choose one of: {choices}"
    )


def format_snakemake_args(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def build_snakemake_command(jobs_to_offload=None) -> str:
    command = f"snakemake {get_snakemake_params()}"
    if jobs_to_offload:
        command += f" --offloader-jobs {','.join(map(str, jobs_to_offload))}"
    return command


def run_checked(command: list[str]) -> str:
    logger.debug("Running command: %s", shlex.join(command))
    result = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.stderr.strip():
        logger.debug(result.stderr.strip())
    return result.stdout


def default_profiling_manifest_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "py-lotaru" / "k8s"


def default_local_profiling_script() -> Path:
    return Path(__file__).resolve().parents[2] / "py-lotaru" / "lotaru-g.sh"


def default_remote_profiling_cache_file() -> Path:
    return Path(".sou") / "latest_remote_profiling.csv"


def run_local_profiling(
    script_path: Path,
    timeout_seconds: int,
    full_scores: bool = False,
    contention: bool = False,
) -> dict[str, float]:
    if not script_path.exists():
        raise FileNotFoundError(f"Missing local profiling script: {script_path}")

    with tempfile.TemporaryDirectory(prefix="sou-local-profile-") as tmp_dir:
        out_dir = Path(tmp_dir) / "out"
        scratch_dir = Path(tmp_dir) / "scratch"
        out_dir.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["OUT"] = str(out_dir)
        env["SCRATCH"] = str(scratch_dir)

        logger.info(
            "Starting local profiling using '%s' (scores_full=%s, contention=%s)",
            script_path,
            full_scores,
            contention,
        )
        script_command = ["bash", str(script_path)]
        if contention:
            script_command.append("--contention")
        if full_scores:
            script_command.append("--scores-full")

        result = subprocess.run(
            script_command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_seconds,
        )
        if result.stderr.strip():
            logger.debug(result.stderr.strip())

        json_files = sorted(out_dir.glob("*.rich.json"))
        if not json_files:
            raise FileNotFoundError(
                "Local profiling did not produce any *.rich.json file"
            )
        with open(json_files[0], encoding="utf-8") as f:
            data = json.load(f)

        local_profile = {
            "node": str(data.get("node", "local")),
            "cpu_score": float(data["cpu_events_s"]),
            "io_score": float(data["io_score"]),
            "memory_score": float(data.get("memory_score", data.get("ram_score", 0.0))),
            "network_score_mbps": float(data.get("network_score_mbps", 0.0)),
        }
        logger.info("Local profiling results: %s", local_profile)
        return local_profile


def configured_network_peer_map() -> str:
    clusters = (get_config() or {}).get("clusters", {})
    peer_map: list[str] = []

    for cluster_name, cluster in clusters.items():
        nodes = cluster.get("nodes", [])
        if len(nodes) < 2:
            raise ValueError(
                f"Cluster '{cluster_name}' needs at least two nodes for network profiling"
            )
        for index, node in enumerate(nodes):
            peer = nodes[(index + 1) % len(nodes)]
            peer_map.append(f'{node["name"]}={peer["ip_address"]}')

    if not peer_map:
        raise ValueError("No cluster nodes configured for network profiling")
    return ",".join(peer_map)


def run_remote_profiling(
    kube_contexts: list[str],
    manifest_dir: Path,
    namespace: str,
    timeout_seconds: int,
    local_profile: dict[str, float] | None = None,
    cache_file: Path | None = None,
    use_cached: bool = False,
    full_scores: bool = False,
    contention: bool = False,
    network_peer_map: str | None = None,
) -> str:
    if use_cached and cache_file is not None and cache_file.exists():
        cached_csv = cache_file.read_text(encoding="utf-8").strip()
        if cached_csv:
            logger.info("Using cached profiling CSV from '%s'", cache_file)
            return cached_csv
        logger.warning(
            "Cached profiling CSV '%s' is empty; collecting fresh profiling data",
            cache_file,
        )

    daemonset_manifest = manifest_dir / "lotaru-g-daemonset.yaml"
    collector_manifest = manifest_dir / "lotaru-g-results-pod.yaml"
    missing = [
        manifest
        for manifest in [daemonset_manifest, collector_manifest]
        if not manifest.exists()
    ]
    if missing:
        raise FileNotFoundError(
            "Missing profiling manifest(s): " + ", ".join(str(path) for path in missing)
        )

    daemonset_manifest_to_apply = daemonset_manifest
    if full_scores or contention:
        if full_scores and not network_peer_map:
            raise ValueError(
                "--scores-full requires peer IP addresses in the SOU node config"
            )

        daemonset_content = daemonset_manifest.read_text(encoding="utf-8")
        flags = " ".join(
            flag
            for enabled, flag in [
                (contention, "--contention"),
                (full_scores, "--scores-full"),
            ]
            if enabled
        )
        daemonset_content = daemonset_content.replace(
            "lotaru-g.sh &&", f"lotaru-g.sh {flags} &&", 1
        )

        lines = daemonset_content.splitlines()
        volume_mounts_index = next(
            (i for i, line in enumerate(lines) if line.strip() == "volumeMounts:"),
            None,
        )
        if full_scores:
            peer_map_block = [
                "            - name: NET_HOST_MAP",
                f'              value: "{network_peer_map}"',
            ]
            if volume_mounts_index is not None:
                lines[volume_mounts_index:volume_mounts_index] = peer_map_block
            else:
                lines.extend(peer_map_block)

        daemonset_content = "\n".join(lines) + "\n"

        enabled_features = "-".join(
            feature
            for enabled, feature in [
                (contention, "contention"),
                (full_scores, "scores-full"),
            ]
            if enabled
        )
        patched_manifest = Path(".sou") / f"lotaru-g-daemonset.{enabled_features}.yaml"
        patched_manifest.parent.mkdir(parents=True, exist_ok=True)
        patched_manifest.write_text(daemonset_content, encoding="utf-8")
        daemonset_manifest_to_apply = patched_manifest
        logger.info(
            "Using patched daemonset manifest (contention=%s, scores_full=%s, NET_HOST_MAP=%s): %s",
            contention,
            full_scores,
            network_peer_map,
            daemonset_manifest_to_apply,
        )

    combined_rows_by_node: dict[str, dict[str, float | str]] = {}

    for context in kube_contexts:
        logger.info("Ensuring result collector exists in context '%s'", context)
        collector_exists = (
            subprocess.run(
                [
                    "kubectl",
                    "--context",
                    context,
                    "-n",
                    namespace,
                    "get",
                    "pod/lotaru-benchmark-results",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).returncode
            == 0
        )
        if collector_exists:
            logger.info("Result collector already exists in context '%s'", context)
        else:
            run_checked(
                [
                    "kubectl",
                    "--context",
                    context,
                    "-n",
                    namespace,
                    "apply",
                    "-f",
                    str(collector_manifest),
                ]
            )
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "wait",
                "--for=condition=Ready",
                "pod/lotaru-benchmark-results",
                "--timeout=120s",
            ]
        )

    context_nodes: dict[str, list[str]] = {}
    for context in kube_contexts:
        node_text = run_checked(
            [
                "kubectl",
                "--context",
                context,
                "get",
                "nodes",
                "-o",
                'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}',
            ]
        )
        nodes = [line.strip() for line in node_text.splitlines() if line.strip()]
        if not nodes:
            raise RuntimeError(f"No nodes found in context '{context}'")
        context_nodes[context] = nodes

    for context in kube_contexts:
        nodes = context_nodes[context]
        logger.info("Clearing previous profiling results in context '%s'", context)
        rich_paths = " ".join(
            shlex.quote(f"/results/lotaru-benchmark/{node}.rich.json") for node in nodes
        )
        csv_path = shlex.quote(f"/results/lotaru-benchmark/lotaru-g-{context}.csv")
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "exec",
                "lotaru-benchmark-results",
                "--",
                "sh",
                "-c",
                f"rm -f {rich_paths} {csv_path}",
            ]
        )

    for context in kube_contexts:
        logger.info("Starting remote profiling in context '%s'", context)
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "apply",
                "-f",
                str(daemonset_manifest_to_apply),
            ]
        )
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "rollout",
                "restart",
                "daemonset/lotaru-g-benchmark",
            ]
        )
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "rollout",
                "status",
                "daemonset/lotaru-g-benchmark",
                f"--timeout={timeout_seconds}s",
            ]
        )

    for context in kube_contexts:
        nodes = context_nodes[context]
        deadline = time.monotonic() + timeout_seconds
        rows: list[dict[str, float | str]] = []

        while time.monotonic() < deadline:
            rows = []
            missing = []
            for node in nodes:
                try:
                    raw = run_checked(
                        [
                            "kubectl",
                            "--context",
                            context,
                            "-n",
                            namespace,
                            "exec",
                            "lotaru-benchmark-results",
                            "--",
                            "cat",
                            f"/results/lotaru-benchmark/{node}.rich.json",
                        ]
                    )
                    data = json.loads(raw)
                except (subprocess.CalledProcessError, json.JSONDecodeError):
                    missing.append(node)
                    continue

                rows.append(
                    {
                        "node": data.get("node", node),
                        "cpu_score": data.get("cpu_events_s", ""),
                        "io_score": data.get("io_score", ""),
                        "memory_score": data.get("memory_score", data.get("ram_score", "")),
                        "network_score_mbps": data.get("network_score_mbps", ""),
                        "contention_score": data.get("contention_score", ""),
                    }
                )

            if not missing and rows:
                break
            time.sleep(5)
        else:
            raise TimeoutError(
                f"No complete profiling results found for context '{context}' within {timeout_seconds}s"
            )

        # Build context-local CSV from expected node files only.
        csv_lines = ["node,cpu_score,io_score,memory_score,network_score_mbps,contention_score"]
        for row in sorted(rows, key=lambda r: str(r["node"])):
            csv_lines.append(
                f"{row['node']},{row['cpu_score']},{row['io_score']},{row['memory_score']},{row['network_score_mbps']},{row['contention_score']}"
            )
        csv = "\n".join(csv_lines)

        csv_target = f"/results/lotaru-benchmark/lotaru-g-{context}.csv"
        write_cmd = f"cat > {shlex.quote(csv_target)} <<'EOF'\n{csv}\nEOF"
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "exec",
                "lotaru-benchmark-results",
                "--",
                "sh",
                "-c",
                write_cmd,
            ]
        )

        logger.info("Profiling results for '%s':\n%s", context, csv.strip())
        logger.info(
            "Wrote context-local profiling CSV for '%s' to %s", context, csv_target
        )
        factors = sou.scheduler.Scheduler.compute_node_runtime_factors_from_csv(csv)
        if factors:
            logger.info("Node runtime factors for '%s': %s", context, factors)

        for row in rows:
            node_name = str(row.get("node", "")).strip()
            if not node_name:
                continue
            combined_rows_by_node[node_name] = {
                "node": node_name,
                "cpu_score": row.get("cpu_score", ""),
                "io_score": row.get("io_score", ""),
                "memory_score": row.get("memory_score", ""),
                "network_score_mbps": row.get("network_score_mbps", ""),
                "contention_score": row.get("contention_score", ""),
            }

        if local_profile is not None:
            local_remote_ratios = (
                sou.scheduler.Scheduler.compute_local_to_remote_ratios_from_csv(
                    local_profile["cpu_score"],
                    local_profile["io_score"],
                    csv,
                )
            )
            if local_remote_ratios:
                logger.info(
                    "Local-vs-remote ratios for '%s' (local node '%s'): %s",
                    context,
                    local_profile["node"],
                    local_remote_ratios,
                )

        logger.info("Cleaning up profiling pods in context '%s'", context)
        run_checked(
            [
                "kubectl",
                "--context",
                context,
                "-n",
                namespace,
                "delete",
                "daemonset/lotaru-g-benchmark",
                "pod/lotaru-benchmark-results",
                "--ignore-not-found=true",
            ]
        )

    combined_csv_lines = ["node,cpu_score,io_score,memory_score,network_score_mbps,contention_score"]
    for node in sorted(combined_rows_by_node):
        row = combined_rows_by_node[node]
        combined_csv_lines.append(
            f"{row['node']},{row['cpu_score']},{row['io_score']},{row['memory_score']},{row['network_score_mbps']},{row['contention_score']}"
        )
    combined_csv = "\n".join(combined_csv_lines)

    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(combined_csv + "\n", encoding="utf-8")
        logger.info("Saved combined profiling CSV cache to '%s'", cache_file)

    logger.info("Combined profiling CSV across contexts:\n%s", combined_csv)
    return combined_csv


def run_tui(snakemake_args: list[str]):
    set_snakemake_params(format_snakemake_args(snakemake_args))
    app = SnakemakeOffloadingUtility()
    command = app.run()
    if command is None:
        print("SOU exited without executing Snakemake.")
        return 1
    print(f"Executing Snakemake. Jobs selected for offloading: {app.jobs_to_offload}")
    print(f"Command: '{command}'")
    return run_command(command, live=True)


def run_cli(argv: list[str]):
    if "--" in argv:
        separator_index = argv.index("--")
        sou_args = argv[:separator_index]
        snakemake_args = argv[separator_index + 1 :]
    else:
        sou_args = argv
        snakemake_args = None

    parser = argparse.ArgumentParser(
        prog="sou --cli",
        description="Run SOU without the TUI and print logs to the terminal.",
    )
    parser.add_argument("--cli", action="store_true")
    parser.add_argument(
        "--exec",
        action="store_true",
        help="execute Snakemake; with --runs-dir executes after prediction, without --runs-dir runs directly without prediction/offloading selection",
    )
    parser.add_argument("--runs-dir")
    parser.add_argument("--deadline-minutes", type=float)
    parser.add_argument("--corr-threshold", type=float, default=0.8)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="terminal log level for CLI mode",
    )
    parser.add_argument(
        "--offloading-strategy",
        default=OffloadingStrategy.NONE.value,
        type=lambda value: parse_enum(OffloadingStrategy, value),
        help="offloading strategy (also accepts short codes: none, pefo, ljf, sisf)",
    )
    parser.add_argument(
        "--profiling-environment",
        default=ProfilingEnvironment.NONE.value,
        type=lambda value: parse_enum(ProfilingEnvironment, value),
    )
    parser.add_argument(
        "--remote-profiling",
        action="store_true",
        help="profile configured Kubernetes clusters with Lotaru-G and print the CSV results",
    )
    parser.add_argument(
        "--local-profiling",
        action="store_true",
        help="profile the local laptop with Lotaru-G and optionally compute local-vs-remote ratios",
    )
    parser.add_argument(
        "--kube-context",
        dest="kube_contexts",
        action="append",
        default=[],
        help="Kubernetes context to profile; can be passed multiple times",
    )
    parser.add_argument(
        "--profiling-manifest-dir",
        default=str(default_profiling_manifest_dir()),
        help="directory containing Lotaru-G Kubernetes manifests",
    )
    parser.add_argument(
        "--local-profiling-script",
        default=str(default_local_profiling_script()),
        help="path to local Lotaru-G profiling script",
    )
    parser.add_argument(
        "--profiling-namespace",
        default="default",
        help="Kubernetes namespace for profiling resources",
    )
    parser.add_argument(
        "--profiling-timeout-seconds",
        type=int,
        default=900,
        help="maximum time to wait for profiling CSV results per context",
    )
    parser.add_argument(
        "--contention",
        action="store_true",
        help="run profiling with additional contended benchmarks and contention metrics",
    )
    parser.add_argument(
        "--scores-full",
        action="store_true",
        help="run profiling in full-score mode (includes memory_score and network_score_mbps)",
    )

    parser.add_argument(
        "--profiling-cache-file",
        default=str(default_remote_profiling_cache_file()),
        help="path to cached combined remote profiling CSV",
    )
    parser.add_argument(
        "--use-cached-profiling",
        action="store_true",
        help="reuse cached profiling CSV instead of rerunning remote profiling benchmarks",
    )

    if snakemake_args is None:
        args, snakemake_args = parser.parse_known_args(sou_args)
    else:
        args = parser.parse_args(sou_args)

    log_level = getattr(logging, args.log_level)
    logger.setLevel(log_level)
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    benchmark_csv = None
    profiling_cache_file = Path(args.profiling_cache_file)
    if args.remote_profiling or args.local_profiling or args.use_cached_profiling:
        if args.remote_profiling and not args.kube_contexts and not args.use_cached_profiling:
            parser.error("--remote-profiling requires at least one --kube-context (unless --use-cached-profiling is set)")



        local_profile = None
        try:
            if args.local_profiling:
                local_profile = run_local_profiling(
                    Path(args.local_profiling_script),
                    args.profiling_timeout_seconds,
                    full_scores=args.scores_full,
                    contention=args.contention,
                )

            if args.remote_profiling:
                benchmark_csv = run_remote_profiling(
                    args.kube_contexts,
                    Path(args.profiling_manifest_dir),
                    args.profiling_namespace,
                    args.profiling_timeout_seconds,
                    local_profile=local_profile,
                    cache_file=profiling_cache_file,
                    use_cached=args.use_cached_profiling,
                    full_scores=args.scores_full,
                    contention=args.contention,
                    network_peer_map=(
                        configured_network_peer_map() if args.scores_full else None
                    ),
                )
            elif args.use_cached_profiling:
                if not profiling_cache_file.exists():
                    parser.error(
                        f"cached profiling file not found: {profiling_cache_file}. Run with --remote-profiling once or provide --profiling-cache-file."
                    )
                benchmark_csv = profiling_cache_file.read_text(encoding="utf-8").strip()
                if not benchmark_csv:
                    parser.error(
                        f"cached profiling file is empty: {profiling_cache_file}."
                    )
                logger.info(
                    "Using cached profiling CSV from '%s'", profiling_cache_file
                )
        except subprocess.CalledProcessError as error:
            message = (error.stderr or error.stdout or str(error)).strip()
            logger.error("Profiling failed: %s", message)
            return error.returncode
        except (FileNotFoundError, TimeoutError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
            logger.error("Profiling failed: %s", error)
            return 1

        # profiling-only mode
        if not args.runs_dir and not snakemake_args:
            return 0

    if not snakemake_args:
        parser.error("missing Snakemake arguments; pass them after '--'")

    set_snakemake_params(format_snakemake_args(snakemake_args))

    if not args.runs_dir:
        if not args.exec:
            parser.error(
                "--runs-dir is required unless --exec is used for direct execution"
            )
        command = build_snakemake_command()
        print(
            "No --runs-dir provided. Skipping prediction and executing Snakemake directly."
        )
        print(f"Command: '{command}'")
        return run_command(command, live=True)

    if (
        args.profiling_environment == ProfilingEnvironment.REMOTE
        and benchmark_csv is None
    ):
        parser.error(
            "profiling environment 'REMOTE' requires --remote-profiling (or --use-cached-profiling) when running prediction in CLI mode"
        )

    deadline_seconds = args.deadline_minutes * 60 if args.deadline_minutes else None
    if (
        args.offloading_strategy
        in {
            OffloadingStrategy.LONGEST_JOB_FIRST,
            OffloadingStrategy.SMALLEST_INPUT_SIZE_FIRST,
        }
        and deadline_seconds is None
    ):
        parser.error("selected offloading strategy requires --deadline-minutes")

    result = sou.scheduler.run_main_safely(
        args.runs_dir,
        deadline_seconds,
        args.offloading_strategy,
        args.profiling_environment,
        args.corr_threshold,
        benchmark_csv,
    )
    if not result:
        return 1

    runtime, offloaded_jobs, cost = result
    print("")
    print(f"Predicted workflow makespan: {runtime / 60:.2f} minutes")
    if args.offloading_strategy != OffloadingStrategy.NONE:
        if deadline_seconds:
            if runtime > deadline_seconds:
                print(
                    f"Makespan prediction exceeds deadline of {deadline_seconds / 60:.2f} minutes"
                )
            else:
                print(
                    f"Makespan prediction meets deadline of {deadline_seconds / 60:.2f} minutes"
                )
        print(f"Predicted offloading costs: ${cost:.2f}")
    print(f"Jobs selected for offloading: {offloaded_jobs}")

    command = build_snakemake_command(offloaded_jobs)
    print(f"Command: '{command}'")
    if not args.exec:
        print("Prediction finished. Snakemake was not executed; pass --exec to run it.")
        return 0
    return run_command(command, live=True)


def main():
    if not Path(".sou").exists():
        Path(".sou").mkdir()

    argv = sys.argv[1:]
    if "--cli" in argv:
        return run_cli(argv)
    return run_tui(argv)
