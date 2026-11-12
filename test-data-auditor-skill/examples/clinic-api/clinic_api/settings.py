import os

# Read once, at import: a test that changes the variable has to reload the module to see it.
CURRENCY = os.getenv("CLINIC_CURRENCY", "AUD")
