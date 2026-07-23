#!/usr/bin/env python3
"""Prepare an isolated StainedGlass reproduction workdir and CHM13 partitions."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

SOURCE = Path("/srv/nfs/snakemake-siena06/niklas/workflow-runs/stained-glass")
DEST = Path("/srv/nfs/snakemake-siena06/niklas/workflow-runs/stained-glass-reproduction")
SOURCE_FASTA = SOURCE / "resources/chm13v2.0.fa"

PARTITIONS = {
    "training_1": ["chr1", "chr3", "chr5", "chr7", "chr9", "chr11", "chr13", "chr15", "chr17"],
    "training_2": ["chr2", "chr4", "chr6", "chr8", "chr10", "chr12", "chr14", "chr16", "chr18", "chr20"],
    "training_3": ["chr4", "chr6", "chr8", "chr10", "chr12", "chr14", "chr16", "chr18", "chr20", "chr22"],
    "training_4": ["chr5", "chr7", "chr9", "chr11", "chr13", "chr15", "chr17", "chr19", "chr21", "chrX"],
    "training_5": ["chr6", "chr8", "chr10", "chr12", "chr14", "chr16", "chr18", "chr20", "chr22", "chrY"],
    "evaluation": ["chr3", "chr5", "chr7", "chr9", "chr11", "chr13", "chr15", "chr17", "chr19"],
}

HISTORICAL_ALL = '''rule all:
    input:
        sort=expand("results/{SM}.{W}.{F}.sorted.bam", SM=SM, W=W, F=F),
        beds=expand("results/{SM}.{W}.{F}.bed.gz", SM=SM, W=W, F=F),
        fulls=expand("results/{SM}.{W}.{F}.full.tbl.gz", SM=SM, W=W, F=F),
'''

REPRODUCTION_ALL = '''rule all:
    input:
        sort=expand("results/{SM}.{W}.{F}.sorted.bam", SM=SM, W=W, F=F),
        identity=expand("temp/{SM}.{W}.{F}.tbl.gz", SM=SM, W=W, F=F),
'''


def read_fai(path: Path) -> dict[str, int]:
    entries = {}
    with path.open() as handle:
        for line in handle:
            name, length, *_ = line.split("\t")
            entries[name] = int(length)
    return entries


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_config(path: Path, fasta_name: str, chromosomes: list[str]) -> None:
    names = ", ".join(f'"{name}"' for name in chromosomes)
    path.write_text(
        "sample: human\n"
        f"fasta: resources/{fasta_name}\n"
        f"names: [{names}]\n"
        "window: 5000\n"
        "nbatch: 18\n"
        "alnthreads: 18\n"
        "mm_f: 100\n"
        "mm_s: 50\n"
        "tempdir: temp\n"
    )


def main() -> None:
    if DEST.exists():
        raise SystemExit(f"Refusing to overwrite existing destination: {DEST}")
    if not SOURCE_FASTA.is_file() or not SOURCE_FASTA.with_suffix(".fa.fai").is_file():
        raise SystemExit(f"Missing indexed source FASTA: {SOURCE_FASTA}")

    (DEST / "resources").mkdir(parents=True)
    (DEST / "config/partitions").mkdir(parents=True)
    shutil.copytree(SOURCE / "workflow", DEST / "workflow")
    for filename in ("README.md", "LICENSE", "Dockerfile.stainedglass"):
        source_file = SOURCE / filename
        if source_file.exists():
            shutil.copy2(source_file, DEST / filename)

    snakefile = DEST / "workflow/Snakefile"
    snakefile_text = snakefile.read_text()
    if HISTORICAL_ALL not in snakefile_text:
        raise SystemExit("Current Snakefile target block was not recognized")
    snakefile.write_text(snakefile_text.replace(HISTORICAL_ALL, REPRODUCTION_ALL, 1))

    source_lengths = read_fai(SOURCE_FASTA.with_suffix(".fa.fai"))
    checksums = []
    manifest = [
        "source_assembly=T2T-CHM13_v2.0",
        "source_fasta=/srv/nfs/snakemake-siena06/niklas/workflow-runs/stained-glass/resources/chm13v2.0.fa",
        "window=5000",
        "nbatch=18",
        "alnthreads=18",
        "mm_f=100",
        "mm_s=50",
        "historical_target_jobs=45",
        "",
    ]

    for partition, chromosomes in PARTITIONS.items():
        fasta_name = f"chm13v2.0_{partition}.fa"
        fasta_path = DEST / "resources" / fasta_name
        config_path = DEST / "config/partitions" / f"config.{partition}.yaml"

        with fasta_path.open("wb") as output:
            subprocess.run(
                ["samtools", "faidx", str(SOURCE_FASTA), *chromosomes],
                stdout=output,
                check=True,
            )
        subprocess.run(["samtools", "faidx", str(fasta_path)], check=True)

        partition_lengths = read_fai(fasta_path.with_suffix(".fa.fai"))
        expected_lengths = {name: source_lengths[name] for name in chromosomes}
        if partition_lengths != expected_lengths:
            raise SystemExit(
                f"Partition verification failed for {partition}: "
                f"expected {expected_lengths}, got {partition_lengths}"
            )

        write_config(config_path, fasta_name, chromosomes)
        checksums.append(f"{sha256(fasta_path)}  {fasta_path.relative_to(DEST)}")
        checksums.append(
            f"{sha256(fasta_path.with_suffix('.fa.fai'))}  "
            f"{fasta_path.with_suffix('.fa.fai').relative_to(DEST)}"
        )
        manifest.append(f"{partition}={','.join(chromosomes)}")

    (DEST / "resources/SHA256SUMS").write_text("\n".join(checksums) + "\n")
    (DEST / "REPRODUCTION-MANIFEST.txt").write_text("\n".join(manifest) + "\n")

    profile = DEST / "workflow/profiles/reproduction/config.yaml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "use-conda: true\n"
        "show-failed-logs: true\n"
        "benchmark-extended: true\n"
        "jobs: 1000\n"
        "seconds-between-status-checks: 2\n"
        "max-threads: 30\n"
        "retries: 6\n"
        "rerun-incomplete: true\n"
        "executor: offloader\n"
        "offloader-primary-comp-env: kubernetes:cluster1\n"
        "offloader-persistent-volumes: snakemake-siena06-pvc:/srv/nfs/snakemake-siena06\n"
        f"offloader-shared-workdir: {DEST}\n"
    )

    for directory in DEST.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o2775)
    for file in DEST.rglob("*"):
        if file.is_file():
            file.chmod(0o664)

    print(f"Prepared {DEST}")
    print("Generated and verified 5 training partitions and 1 evaluation partition")


if __name__ == "__main__":
    main()
