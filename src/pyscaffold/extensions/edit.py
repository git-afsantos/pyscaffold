import os
import shlex
import textwrap
from argparse import Action, ArgumentParser
from functools import lru_cache, reduce
from itertools import chain
from typing import List, Optional, Set

from .. import api, cli, file_system, shell, templates
from ..actions import ScaffoldOpts as Opts
from ..actions import get_default_options
from . import Extension, iterate_entry_points

INDENT_LEVEL = 4
HEADER = templates.get_template("header_edit")


CONFIG = {
    "ignore": ["--help", "--version"],
    "comment": ["--verbose", "--very-verbose"],
}
"""Configuration for the options that are not associated with an extension class.

This dict is used by the :obj:`get_config` function, and will be augmented
by each extension via the ``on_edit`` attribute.
"""


@lru_cache(maxsize=2)
def get_config(kind: str) -> Set[str]:
    """Get configurations that will be used for generating examples.

    The ``kind`` argument can assume 2 values, and will result in a different output:

    - ``"ignore"``: Options that should be simply ignored when creating examples
    - ``"comment"``: Options that should be commented when creating examples,
        even if they appear in the original ``sys.argv``.
    """
    # TODO: when `python_requires >= 3.8` use Literal["ignore", "comment"] instead of
    #       str for type annotation of kind
    assert kind in CONFIG.keys()
    initial_value = set(CONFIG[kind])

    def _reducer(acc, ext):
        config_from_ext = getattr(ext, "on_edit", {"ignore": [], "comment": []})
        return acc | set(config_from_ext.get(kind, []))

    return reduce(_reducer, iterate_entry_points(), initial_value)


class Edit(Extension):
    """Allows to user to choose PyScaffold's options by editing a file with examples."""

    parser: ArgumentParser
    on_edit = {"ignore": ["--edit"]}

    def augment_cli(self, parser: ArgumentParser):
        """See :obj:`~pyscaffold.extension.Extension.augment_cli`."""
        self.parser = parser

        parser.add_argument(
            self.flag,
            dest="command",
            action="store_const",
            const=self.command,
            help=self.help_text,
        )
        return self

    def command(self, opts: Opts):
        """This method replace the regular call to :obj:`cli.run_scaffold` with an
        intermediate file to confirm the user's choices in terms of arguments/options.
        """
        opts = expand_computed_opts(opts)
        examples = all_examples(self.parser, self.parser._actions, opts)
        content = (os.linesep * 2).join([HEADER.template, examples])
        with file_system.tmpfile(prefix="pyscaffold-", suffix=".args.sh") as file:
            file.write_text(content, "utf-8")
            content = shell.edit(file).read_text("utf-8")
            cli.main(split_args(content))  # Call the CLI again with the new options


def expand_computed_opts(opts: Opts) -> Opts:
    _struct, opts = get_default_options({}, api.bootstrap_options(opts))
    return opts


def wrap(text: Optional[str], width=70, **kwargs) -> str:
    return os.linesep.join(textwrap.wrap(text or "", width, **kwargs))


def comment(text: str, comment_mark="#", indent_level=0):
    return textwrap.indent(text, (" " * indent_level) + comment_mark + " ")


def join_block(*parts: str, sep=os.linesep):
    return sep.join(p for p in parts if p)


def long_option(action: Action):
    return sorted(action.option_strings or [""], key=len)[-1]


def alternative_flags(action: Action):
    opts = sorted(action.option_strings, key=len)[:-1]
    return f"(or alternatively: {' '.join(opts)})" if opts else ""


def has_active_extension(action: Action, opts: Opts) -> bool:
    ext_flags = [getattr(ext, "flag", None) for ext in opts.get("extensions", [])]
    return any(f in ext_flags for f in action.option_strings)


def example_no_value(parser: ArgumentParser, action: Action, opts: Opts) -> str:
    long = long_option(action)
    if (
        long not in get_config("comment")
        and (action.dest != "extensions" and opts.get(action.dest))
        or has_active_extension(action, opts)
    ):
        return f" {long}"

    return comment(long)


def example_with_value(parser: ArgumentParser, action: Action, opts: Opts) -> str:
    long = long_option(action)
    arg = opts.get(action.dest)
    args = arg if isinstance(arg, (list, tuple)) else [arg]
    value = " ".join(shlex.quote(f"{a}") for a in args).strip()

    if arg is None or long in get_config("comment") or value == "":
        formatter = parser._get_formatter()
        return comment(f"{long} {formatter._format_args(action, action.dest)}".strip())

    return f" {long} {value}"


def example(parser: ArgumentParser, action: Action, opts: Opts) -> str:
    fn = example_no_value if action.nargs == 0 else example_with_value
    return fn(parser, action, opts)


def example_with_help(parser: ArgumentParser, action: Action, opts: Opts) -> str:
    return join_block(
        example(parser, action, opts),
        comment(alternative_flags(action), indent_level=INDENT_LEVEL),
        comment(wrap(action.help), indent_level=INDENT_LEVEL),
    )


def all_examples(parser: ArgumentParser, actions: List[Action], opts: Opts) -> str:
    parts = (
        example_with_help(parser, a, opts)
        for a in actions
        if long_option(a) not in get_config("ignore")
    )
    return join_block(*parts, sep=os.linesep * 3)


def split_args(text: str) -> List[str]:
    lines = (line.strip() for line in text.splitlines())
    return list(chain.from_iterable(shlex.split(x) for x in lines if x and x[0] != "#"))