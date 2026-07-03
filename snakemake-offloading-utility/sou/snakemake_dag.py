import json
import logging
from pathlib import Path

import networkx as nx

from sou.helpers import get_snakemake_params, run_command

D3DAG_CACHE = ".sou/d3dag.json"

logger = logging.getLogger("sou")


class SnakemakeDag:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized") or not Path(D3DAG_CACHE).exists():
            self.d3dag = self._get_d3dag_from_snakemake()
            self.dag = self._build_dag()
            self._initialize_node_attributes()
            self.rule_graph = self._build_rule_graph()
            self.initialized = True

    def _get_d3dag_from_snakemake(self):
        if Path(D3DAG_CACHE).exists():
            logger.info(f"Reading cached d3dag file from '{D3DAG_CACHE}'")
            with open(Path(D3DAG_CACHE)) as f:
                return json.load(f)
        else:
            logger.info(f"Retrieving snakemake DAG...")
            command = f"snakemake {get_snakemake_params()} --d3dag"
            result = json.loads(run_command(command, False))
            Path(D3DAG_CACHE).parent.mkdir(parents=True, exist_ok=True)
            with open(Path(D3DAG_CACHE), "w") as f:
                json.dump(result, f, indent=2)
            return result

    def _build_dag(self):
        G = nx.DiGraph()

        for node in self.d3dag["nodes"]:
            jobid = node["value"]["jobid"]
            G.add_node(jobid, **node["value"])

        for edge in self.d3dag["edges"]:
            u_jobid = next(n for n in self.d3dag["nodes"] if n["id"] == edge["u"])[
                "value"
            ]["jobid"]
            v_jobid = next(n for n in self.d3dag["nodes"] if n["id"] == edge["v"])[
                "value"
            ]["jobid"]

            G.add_edge(u_jobid, v_jobid)

        logger.info("Job DAG successfully built")
        return G

    def _initialize_node_attributes(self):
        for node in self.dag.nodes:
            self.dag.nodes[node]["completed"] = False
            if self.dag.in_degree(node) == 0:
                self.dag.nodes[node]["runnable"] = True
            else:
                self.dag.nodes[node]["runnable"] = False

    def _build_rule_graph(self):
        rule_graph = nx.DiGraph()
        jobid_to_rule = {
            node["id"]: node["value"]["rule"] for node in self.d3dag["nodes"]
        }

        for edge in self.d3dag["edges"]:
            from_rule = jobid_to_rule[edge["u"]]
            to_rule = jobid_to_rule[edge["v"]]

            if from_rule != to_rule:
                rule_graph.add_edge(from_rule, to_rule)

        logger.info("Rule graph successfully built")
        return rule_graph

    def get_jobids(self):
        return self.dag.nodes

    def job_has_label(self, jobid: int, label: str) -> bool:
        if jobid in self.dag.nodes:
            return self.dag.nodes[jobid].get("label") == label
        else:
            logger.warning(f"Job {jobid} not found in DAG")
            return False

    def get_rule_by_jobid(self, jobid):
        return self.dag.nodes[jobid]["rule"]

    def get_ancestors_by_rule(self, rule):
        try:
            ancestors = nx.ancestors(self.rule_graph, rule)
            ancestors.add(rule)
            # logger.debug(f"Ancestors of {rule}: {ancestors}")
            return ancestors
        except nx.NetworkXError as e:
            logger.error(f"Error retrieving ancestors for {rule}: {e}")
            return set()

    def get_ancestors_by_jobid(self, jobid):
        try:
            ancestors = nx.ancestors(self.dag, jobid)
            ancestors.add(jobid)
            rules_by_ancestors = {a: self.get_rule_by_jobid(a) for a in ancestors}
            # logger.debug(f"Ancestors of {jobid}: '{self.get_rule_by_jobid(jobid)}' : {rules_by_ancestors}")
            return ancestors
        except KeyError as e:
            logger.error(f"Error retrieving ancestors for jobid {jobid}: {e}")
            return set()

    def mark_completed(self, jobid):
        if jobid in self.dag.nodes:
            self.dag.nodes[jobid]["completed"] = True
            # logger.debug(f"Marked job {jobid} {self.dag.nodes[jobid]['rule']} as completed")
            for succ_jobid in self.dag.successors(jobid):
                if not self.dag.nodes[succ_jobid].get("node"):
                    if all(
                        self.dag.nodes[pred]["completed"] == True
                        for pred in self.dag.predecessors(succ_jobid)
                    ):
                        self.dag.nodes[succ_jobid]["runnable"] = True
                        # logger.debug(f"Marked job {succ_jobid} {self.dag.nodes[succ_jobid]['rule']} as runnable")
        else:
            logger.warning(f"Job {jobid} not found in DAG")

    def annotate_nodes_with_files(self, jobids_to_files: dict[str, list[str]]):
        for jobid, files in jobids_to_files.items():
            if jobid in self.dag.nodes:
                self.dag.nodes[jobid]["files"] = files
            else:
                logger.warning(f"Job {jobid} not found in DAG")

        # jobs that don't need to be executed are still part of the DAG but not of the dryrun
        # they can be marked as completed
        for jobid in self.dag.nodes:
            if not self.dag.nodes[jobid].get("files", {}).get("outputs"):
                self.dag.nodes[jobid]["completed"] = True
                self.dag.nodes[jobid]["runnable"] = False

    def has_uncompleted(self):
        for node in self.dag.nodes:
            if not self.dag.nodes[node]["completed"]:
                return True
        return False

    def get_runnable(self) -> list[str]:
        runnable_jobs = [
            jobid for jobid in self.dag.nodes if self.dag.nodes[jobid]["runnable"]
        ]
        for jobid in runnable_jobs:
            # only return every runnable job once
            self.dag.nodes[jobid]["runnable"] = False
        logger.debug(f"New runnable jobs: {runnable_jobs}")
        return runnable_jobs

    def set_cluster_node(self, jobid: int, cluster_node: str):
        if jobid in self.dag.nodes:
            self.dag.nodes[jobid]["cluster_node"] = cluster_node
        else:
            logger.warning(f"Job {jobid} not found in DAG")

    def get_cluster_node(self, jobid: int) -> str | None:
        if jobid in self.dag.nodes:
            if "cluster_node" in self.dag.nodes[jobid]:
                return self.dag.nodes[jobid]["cluster_node"]
            else:
                logger.warning(f"Job {jobid} does not have a cluster node set")
                return None
        else:
            logger.warning(f"Job {jobid} not found in DAG")
            return None

    def get_initial_input_files_all_jobs(self) -> set[str]:
        all_inputs = set()
        all_outputs = set()

        for job in self.dag.nodes:
            all_inputs.update(self.dag.nodes[job].get("files", {}).get("inputs", []))
            all_outputs.update(self.dag.nodes[job].get("files", {}).get("outputs", []))
            all_outputs.update(self.dag.nodes[job].get("files", {}).get("logs", []))

        return all_inputs - all_outputs

    def get_initial_input_files_ancestors(self, jobid: str) -> set[str]:
        initial_input_files_all_jobs = self.get_initial_input_files_all_jobs()
        ancestors = self.get_ancestors_by_jobid(jobid)
        ancestor_input_files = set()

        for ancestor in ancestors:
            if "inputs" in self.dag.nodes[ancestor].get("files", {}):
                ancestor_input_files.update(self.dag.nodes[ancestor]["files"]["inputs"])

        initial_input_files_ancestors = (
            ancestor_input_files & initial_input_files_all_jobs
        )
        # logger.debug(f"Initial input files for ancestors of {jobid}: {initial_input_files_ancestors}")
        return initial_input_files_ancestors

    def reset(self):
        self._initialize_node_attributes()
        self.annotate_nodes_with_files({})
