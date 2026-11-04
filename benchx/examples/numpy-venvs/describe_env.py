"""The collector: print what the numpy that this Python imports says about itself.

benchx.conf.json runs it once per call, in the environment the call runs in, so it can only
differ between the two sides if their environments do. It prints the same text for the same
environment: no paths, no timestamps.
"""

import json

import numpy

print(json.dumps({"summary": {"numpy": numpy.__version__}}, sort_keys=True))
