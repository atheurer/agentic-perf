# Crucible user environment selection workflow

Discover user environments available on the assigned controller with its live
userenv discovery tool. Then inspect the requested benchmark's installed
workshop metadata through get_skill_context to determine whether that
benchmark has explicit support or uses a fallback. Names, image availability,
registry authentication requirements, and compatibility can change; do not use
a hard-coded mapping from an OS name to a userenv.

Prefer an environment explicitly supported by the benchmark and compatible
with the user's OS requirement. State the evidence and confidence behind the
selection. If there is no verified compatible environment, or required image
access cannot be confirmed, request clarification instead of guessing.
