"""Enable `python -m ishelf_ee ...` as an alternative to the `ee` command."""
import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
