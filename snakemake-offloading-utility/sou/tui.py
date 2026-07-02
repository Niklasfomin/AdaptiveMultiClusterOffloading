import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Center, HorizontalGroup
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Static,
)

import sou.scheduler
from sou.helpers import (
    get_snakemake_params,
    run_command,
    set_snakemake_params,
)
from sou.scheduler import OffloadingStrategy


class RichLogHandler(logging.Handler):
    def __init__(self, log_widget: RichLog):
        super().__init__()
        self.log_widget = log_widget

    def emit(self, record):
        msg = self.format(record)
        self.log_widget.write(msg)


logger = logging.getLogger("sou")
logger.setLevel(logging.DEBUG)


class ParameterLabels(HorizontalGroup):
    def compose(self) -> ComposeResult:
        yield Label(" Runs directory:", classes="vertical")
        yield Label(" Correlation threshold:", classes="vertical")
        yield Label("Deadline:", classes="vertical")
        yield Label("Offloading strategy:", classes="vertical")


class Parameters(HorizontalGroup):
    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="/path/to/runs-directory",
            id="runs_dir_input",
            classes="vertical",
        )
        yield Input(
            placeholder="(default: 0.8)",
            id="threshold_input",
            classes="vertical",
            type="number",
            valid_empty=True,
        )
        yield Input(
            placeholder="(in minutes)",
            id="deadline_input",
            classes="vertical",
            type="number",
            valid_empty=True,
        )
        yield OptionList(
            *[s.value for s in OffloadingStrategy],
            id="offloading_strategy_option_list",
            classes="vertical",
            compact=True,
        )


class Buttons(HorizontalGroup):
    def compose(self) -> ComposeResult:
        yield Button(
            "Predict Makespan and Cost",
            id="predict_button",
            classes="vertical",
            variant="success",
        )
        yield Button(
            "Execute Snakemake (Quit SOU)",
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
            yield ParameterLabels()
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

        if event.button.id == "predict_button":
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
                runs_dir, deadline_seconds, offloading_strategy, corr_threshold
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


def main():
    if not Path(".sou").exists():
        Path(".sou").mkdir()

    snakemake_args = sys.argv[1:]
    set_snakemake_params(" ".join(snakemake_args))
    app = SnakemakeOffloadingUtility()
    command = app.run()
    if command is None:
        print("SOU exited without executing Snakemake.")
        return 1
    print(f"Executing Snakemake. Jobs selected for offloading: {app.jobs_to_offload}")
    print(f"Command: '{command}'")
    run_command(command, live=True)
