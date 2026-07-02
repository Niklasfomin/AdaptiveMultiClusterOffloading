import base64
import uuid


UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://github.com/snakemake-offloader/snakemake-executor-plugin-kubernetes",
)


def get_uuid(name):
    return uuid.uuid5(UUID_NAMESPACE, name)


def read_and_base64_encode_file(file_path: str):
    with open(file_path, "r") as key_file:
        key = key_file.read()

    base64_key = base64.b64encode(key.encode()).decode()
    return base64_key


def convert_to_cpus(str_value):
    if str_value.endswith("m"):  # millicpus
        return float(str_value.rstrip("m")) / 1000
    else:
        return float(str_value)


def convert_to_bytes(str_value):
    if str_value.endswith("Ki"):
        return int(float(str_value.rstrip("Ki")) * 1024)
    elif str_value.endswith("Mi"):
        return int(float(str_value.rstrip("Mi")) * 1024**2)
    elif str_value.endswith("Gi"):
        return int(float(str_value.rstrip("Gi")) * 1024**3)
    elif str_value.endswith("K"):
        return int(float(str_value.rstrip("K")) * 1000)
    elif str_value.endswith("M"):
        return int(float(str_value.rstrip("M")) * 1000**2)
    elif str_value.endswith("G"):
        return int(float(str_value.rstrip("G")) * 1000**3)
    elif str_value.isdigit():
        return int(str_value)
    else:
        raise Exception("Could not convert value to bytes: " + str_value)
