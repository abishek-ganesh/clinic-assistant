"""Every threshold and limit the code decides with. Models return numbers; this file decides."""

# Show the fixed 911 message at or above this emergency probability. Deliberately below 0.5:
# a false positive costs one click, a false negative is the failure this guardrail exists for.
EMERGENCY_MIN = 0.35

# Decline when the judgment model is this sure the question is outside the handbook's scope.
IN_SCOPE_MIN = 0.25

# Decline when even the best passage is this dissimilar: nothing relevant was retrieved.
# Measured with scripts/eval_retrieval.py: answerable questions score 0.52 to 0.69 on their
# best passage, out-of-scope and emergency questions 0.14 to 0.37. 0.45 sits in the gap.
SIMILARITY_MIN = 0.45

# Passages handed to the generator.
TOP_K = 4

# Longest question the demo page accepts, in characters.
MAX_QUESTION_CHARS = 500

# Class endpoint: max_tokens when the student sends none, and the most we allow.
DEFAULT_MAX_TOKENS = 1024
MAX_MAX_TOKENS = 4096

# Handbook chunking: target size and hard cap, in approximate tokens.
CHUNK_MAX_TOKENS = 350
