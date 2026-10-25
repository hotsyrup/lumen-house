"""Uninstalled owner-terminal one-shot maintenance. No action by default.

An online host should call MaintenanceRunner with its existing repository.
This terminal path requires the existing writer to be quiesced; it never
migrates a store, changes intake, creates identities or installs a schedule.
"""
import argparse
import json
import os
from pathlib import Path
import stat

from .maintenance import MaintenanceRunner
from .repository import RepositoryError, native_repository_class

FORMAT = 'lumen-house-exchange-maintenance/v1'


def load_configuration(path):
    candidate = Path(path)
    info = candidate.lstat()
    if (not stat.S_ISREG(info.st_mode) or candidate.is_symlink() or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192):
        raise ValueError('Owner-private explicit maintenance configuration required')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate maintenance fields')
            result[key] = value
        return result
    value = json.loads(candidate.read_text(), object_pairs_hook=unique)
    expected = {'format', 'enabled', 'storePath', 'checkpointDirectory', 'cadenceMs'}
    if type(value) is not dict or set(value) != expected or value['format'] != FORMAT or type(value['enabled']) is not bool:
        raise ValueError('Explicit supported maintenance configuration required')
    if value['enabled']:
        if (type(value['cadenceMs']) is not int or value['cadenceMs'] <= 0
                or any(not isinstance(value[key], str) or not Path(value[key]).is_absolute()
                       for key in ('storePath', 'checkpointDirectory'))):
            raise ValueError('Owner must select paths and cadence')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if args.config is None:
        print(json.dumps({'enabled': False, 'ran': False, 'installed': False}))
        return 0
    repository = None
    try:
        config = load_configuration(args.config)
        if not config['enabled'] or not args.execute:
            print(json.dumps({'enabled': config['enabled'], 'ran': False, 'installed': False,
                              'executionRequested': args.execute}))
            return 0
        repository = native_repository_class()(config['storePath'])
        runner = MaintenanceRunner(repository, enabled=True, cadence_ms=config['cadenceMs'],
                                   checkpoint_directory=config['checkpointDirectory'])
        print(json.dumps(runner.run_once(), sort_keys=True))
        return 0
    except Exception:
        # Native StoreError and filesystem/parser failures are also redacted.
        # No path, configuration, principal, terms or exception value is echoed.
        print(json.dumps({'error': 'maintenance_failed', 'ran': False, 'checkpointConfirmed': False,
                          'mutationOutcome': 'may_have_committed'}))
        return 1
    finally:
        if repository is not None:
            repository.close()


if __name__ == '__main__':
    raise SystemExit(main())
