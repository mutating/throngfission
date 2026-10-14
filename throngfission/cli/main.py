"""The command-line entry point; all argument values are provided by skelet."""

import sys

from skelet import EnvSource, FixedCLISource, Storage

from throngfission.cli.settings import SetupSettings
from throngfission.cli.setup import Setup
from throngfission.execution.settings import Settings


class Command(Storage):
    command: str = ''


def main() -> int:
    fields = [field for field in Settings.__annotations__ if field not in ('name', 'image')]
    source = FixedCLISource[object](positional_arguments=['command'], named_arguments=['help', *fields, *SetupSettings.__annotations__])
    source.parser.allow_abbrev = False
    source.parser.description = 'Prepare K3s, Fission and the python, debian and ubuntu runtimes. Usage: throngfission setup [options].'
    # Skelet treats parser failures as a missing source value. Reject malformed
    # input before constructing settings so a typo can never start an installation.
    parsed = vars(source.parser.parse_args())
    for field, value in parsed.items():
        if value is None and field != 'help':
            source.parser.error(f"--{field.replace('_', '-')} requires a value")
    if isinstance(parsed['help'], str):
        source.parser.error('--help does not accept a value')
    command = Command(_sources=[source])
    # Reading the boolean directly avoids skelet 0.0.21's mismatch between
    # the Storage and FixedCLISource sentinels for an absent boolean flag.
    if source.type_awared_get('help', bool) or not command.command:
        sys.stdout.write(source.parser.format_help())
        return 0
    if command.command != 'setup':
        source.parser.error('expected the command: setup')
    sources = [source, EnvSource[object](prefix='THRONGFISSION_')]
    settings = Settings(_sources=sources)
    options = SetupSettings(_sources=sources)
    Setup(settings, options).run()
    return 0
# mypy: disable-error-code=misc
