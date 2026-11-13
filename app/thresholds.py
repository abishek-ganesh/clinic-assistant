"""Every threshold and limit the code decides with. Models return numbers; this file decides."""

# Show the fixed 911 message at or above this emergency probability. Deliberately below 0.5:
# a false positive costs one click, a false negative is the failure this guardrail exists for.
EMERGENCY_MIN = 0.35

# Decline when the judgment model is this sure the question is outside the handbook's scope.
IN_SCOPE_MIN = 0.25

# Decline when the reranker scores even the best passage this low: nothing relevant was found.
# Measured with scripts/eval_retrieval.py (DECISIONS D68): answerable questions score 0.73 to
# 0.94 as full sentences and 0.49 to 0.90 when terse ("where are you?" is the lowest).
# Out-of-scope questions score 0.31 to 0.51, so there is no clean gap. 0.47 keeps every
# answerable question and declines 9 of 11 out-of-scope ones; the two above it ("dentist?",
# "dermatologist near me") are left to the scope judgment and the generator.
RELEVANCE_MIN = 0.47

# Passages vector search hands the reranker, and passages the reranker hands the generator.
RERANK_CANDIDATES = 10
TOP_K = 4

# Longest question the demo page accepts, in characters.
MAX_QUESTION_CHARS = 500

# Class endpoint: max_tokens when the student sends none, and the most we allow.
DEFAULT_MAX_TOKENS = 1024
MAX_MAX_TOKENS = 4096

# Handbook chunking: target size and hard cap, in approximate tokens.
CHUNK_MAX_TOKENS = 350

# Booking agent: the most model calls in one turn. A normal turn takes two or three (look up
# slots, then propose or reply), so hitting this means the model is going in circles.
AGENT_MAX_MODEL_CALLS = 6

# Booking agent: the most slots one check_availability call returns to the model.
AVAILABILITY_MAX_SLOTS = 10

# Booking agent: how long a proposal's signed confirmation stays valid, in seconds.
PROPOSAL_TTL_SECONDS = 600

# Booking page: longest message, most earlier turns sent back, and longest earlier turn.
MAX_BOOKING_MESSAGE_CHARS = 500
MAX_BOOKING_HISTORY = 30
MAX_BOOKING_TURN_CHARS = 2000

# Token page: longest email (the limit in the email standard) and longest class code accepted.
MAX_EMAIL_CHARS = 254
MAX_CLASS_CODE_CHARS = 100

# Token page: how long an emailed link works, and how many links one address can be sent in
# an hour. The second number stops the form being used to flood someone's inbox.
TOKEN_LINK_TTL_SECONDS = 900
MAX_TOKEN_LINKS_PER_HOUR = 3
