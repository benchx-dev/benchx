"""The collector: print the pandas this Python imports, and the libraries that decide how
fast it is.

benchx.conf.json runs it once per call, in the environment the call runs in, so the two sides
can only differ here if their environments do. It prints the same text for the same
environment: no paths, no timestamps.
"""

import json

import numpy
import pandas
import pyarrow

print(json.dumps({"summary": {"pandas": pandas.__version__, "numpy": numpy.__version__,
                              "pyarrow": pyarrow.__version__}}, sort_keys=True))
