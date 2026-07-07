import argparse
import asyncio
import logging
import shlex
import sys
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
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--deadline-minutes", type=float)
    parser.add_argument("--corr-threshold", type=float, default=0.8)
    parser.add_argument(
        "--offloading-strategy",
        default=OffloadingStrategy.NONE.value,
        type=lambda value: parse_enum(OffloadingStrategy, value),
    )
    parser.add_argument(
        "--profiling-environment",
        default=ProfilingEnvironment.NONE.value,
        type=lambda value: parse_enum(ProfilingEnvironment, value),
    )

    if snakemake_args is None:
        args, snakemake_args = parser.parse_known_args(sou_args)
    else:
        args = parser.parse_args(sou_args)

    if not snakemake_args:
        parser.error("missing Snakemake arguments; pass them after '--'")

    if args.profiling_environment in {
        ProfilingEnvironment.LOCAL,
        ProfilingEnvironment.REMOTE,
    }:
        parser.error(
            "selected profiling environment is not supported for CLI execution"
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

    set_snakemake_params(format_snakemake_args(snakemake_args))
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    result = sou.scheduler.run_main_safely(
        args.runs_dir,
        deadline_seconds,
        args.offloading_strategy,
        args.profiling_environment,
        args.corr_threshold,
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
    return run_command(command, live=True)


def main():
    if not Path(".sou").exists():
        Path(".sou").mkdir()

    argv = sys.argv[1:]
    if "--cli" in argv:
        return run_cli(argv)
    return run_tui(argv)
