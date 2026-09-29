"""``python -m hugpy_agent.mct`` -> the Mediated Context Terminal REPL."""
import sys

from hugpy_agent.mct.repl import main

if __name__ == "__main__":
    sys.exit(main())
