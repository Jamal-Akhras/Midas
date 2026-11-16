"""`python -m midas ...` is `midas ...`: the same CLI without the console
launcher pip writes into the Scripts folder, which an application-control
policy (Windows Smart App Control, on 2026-11-16) can refuse to run."""

from midas.cli import main

main()
