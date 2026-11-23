"""IntentQoS -- an intent-aware adaptive QoS engine for mixed home broadband traffic.

Reference implementation for the Tata Elxsi TELIPORT Season 4 case study
"Adaptive QoS Engine for Mixed Home Broadband Traffic".
"""

__version__ = "2.0.0"

# Traffic classes produced by the classifier (header-only, no payload inspection)
LABELS = ("voip", "gaming", "video", "streaming", "web", "bulk")

# Scheduling tiers ("tins"), highest priority first
TIN_INTENT, TIN_VOICE, TIN_VIDEO, TIN_BE, TIN_BULK = 0, 1, 2, 3, 4
TIN_NAMES = {TIN_INTENT: "intent", TIN_VOICE: "voice", TIN_VIDEO: "video", TIN_BE: "besteffort", TIN_BULK: "bulk"}

LABEL_TO_TIN = {
    "voip": TIN_VOICE,
    "gaming": TIN_VOICE,
    "video": TIN_VIDEO,
    "streaming": TIN_BE,   # buffered video tolerates delay but must keep its fair share
    "web": TIN_BE,
    "bulk": TIN_BULK,
    None: TIN_BE,
}
