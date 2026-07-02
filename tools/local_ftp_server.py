import os
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer


def main():
    repo_root = Path(__file__).resolve().parents[1]
    ftp_root = repo_root / "workflow-runs" / "ftp-root"
    ftp_root.mkdir(parents=True, exist_ok=True)

    username = os.environ.get("SNAKEMAKE_STORAGE_FTP_USERNAME", "snakemake")
    password = os.environ.get("SNAKEMAKE_STORAGE_FTP_PASSWORD", "snakemake")
    host = os.environ.get("LOCAL_FTP_HOST", "0.0.0.0")
    port = int(os.environ.get("LOCAL_FTP_PORT", "2121"))
    passive_start = int(os.environ.get("LOCAL_FTP_PASSIVE_START", "30000"))
    passive_end = int(os.environ.get("LOCAL_FTP_PASSIVE_END", "30009"))

    authorizer = DummyAuthorizer()
    authorizer.add_user(username, password, str(ftp_root), perm="elradfmwMT")

    handler = FTPHandler
    handler.authorizer = authorizer
    handler.passive_ports = range(passive_start, passive_end + 1)

    server = FTPServer((host, port), handler)
    print(
        f"Serving FTP root {ftp_root} on {host}:{port} "
        f"with passive ports {passive_start}-{passive_end}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
