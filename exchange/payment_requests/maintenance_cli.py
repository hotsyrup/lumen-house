"""Uninstalled owner-terminal schema13 sweep and checkpoint; no action default.

Reuse the existing owner-private maintenance configuration format and validation.
The store must already be schema13 and its native writer must be quiesced. This
one-shot command never migrates, restores, releases holds or installs a schedule.
"""
import argparse
import json

from ..controls.maintenance_cli import FORMAT, load_configuration
from .maintenance import MaintenanceRunner
from .repository import native_repository_class


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.config is None:
        print(json.dumps({"enabled": False, "ran": False, "installed": False}))
        return 0
    repository = None
    try:
        config = load_configuration(args.config)
        if not config["enabled"] or not args.execute:
            print(json.dumps({"enabled": config["enabled"], "ran": False, "installed": False,
                              "executionRequested": args.execute}))
            return 0
        repository = native_repository_class()(config["storePath"])
        runner = MaintenanceRunner(repository, enabled=True, cadence_ms=config["cadenceMs"],
                                   checkpoint_directory=config["checkpointDirectory"])
        print(json.dumps(runner.run_once(), sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"error": "maintenance_failed", "ran": False, "checkpointConfirmed": False,
                          "mutationOutcome": "may_have_committed"}))
        return 1
    finally:
        if repository is not None:
            repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
