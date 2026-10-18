APPLICATION_VERSION = "0.1.0.0"

MODULE_VERSIONS = {
    "runtime": "1",
    "scheduler": "1",
    "learning": "1",
    "functions": "1",
    "ui": "1",
}


def version_manifest() -> dict:
    return {
        "application": APPLICATION_VERSION,
        "modules": dict(MODULE_VERSIONS),
    }
