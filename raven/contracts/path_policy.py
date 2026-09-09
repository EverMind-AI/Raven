"""The disk-layout invariants raven and its out-of-tree readers hold each other to.

A paper of constants, not a resolver: these values name what may not drift.
``raven.home`` implements the home rule from them; the retired vendored
launchers re-derived the same facts by hand (they ran under bare python3,
unable to import raven), and their git history is a list of the breakages
this paper exists to prevent. The agents launchers that replaced them run on
installed raven and ask it instead. The glossary's rule stands here
in code: resolving the home again anywhere else is how two directories become
the answer to one question.

Two absences are deliberate. Path-escape checking is a security boundary
owned by the tools (``agent/tools/filesystem.resolve_path``); welding it into
a paper would force one implementation across packages. And there is no
callable protocol yet: the facts below have readers today, a typed interface
does not -- it joins when a consumer types against it, not before.
"""

#: The environment variable that relocates raven's home. A set-but-blank
#: value is unset (a shell profile's ``RAVEN_HOME=`` must not mean "here").
HOME_ENV_VAR = "RAVEN_HOME"

#: Directory under the user's home that is the default home.
DEFAULT_HOME_DIRNAME = ".raven"

#: The configuration file at the home root. Its parent directory doubles as
#: the instance data directory (``config/paths.get_data_dir``): a second
#: instance pointed at its own config file gets its own data tree with no
#: second knob.
CONFIG_FILENAME = "config.json"

#: The raw ``agents.defaults.workspace`` value that means "the default":
#: exactly this string resolves to ``<home>/workspace`` so the workspace
#: follows a relocated home; any other value is the user's own path.
WORKSPACE_DEFAULT_SENTINEL = "~/.raven/workspace"

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_HOME_DIRNAME",
    "HOME_ENV_VAR",
    "WORKSPACE_DEFAULT_SENTINEL",
]

__tier__ = "contract"
