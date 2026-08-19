from dataclasses import dataclass
from typing import Optional

@dataclass
class PerceptionSnapshot:
    """
    A single, unified snapshot of what Eli currently perceives.
    This is the OUTPUT of fusion -- other modules (behavior, conversation)
    should read from this instead of touching vision/audio modules directly.
    """
    person_name: Optional[str] = None       # None if unknown or no face
    emotion: Optional[str] = None            # None if no face / not checked yet
    heard_text: Optional[str] = None         # None if nothing said recently

    def describe(self):
        """A human-readable summary, useful for debugging/logging."""
        parts = []
        if self.person_name:
            parts.append(f"person={self.person_name}")
        else:
            parts.append("person=unknown")

        if self.emotion:
            parts.append(f"emotion={self.emotion}")

        if self.heard_text:
            parts.append(f"heard='{self.heard_text}'")

        return " | ".join(parts) if parts else "no perception data"


def fuse(person_name=None, emotion=None, heard_text=None):
    """
    Combines individual modality outputs into one PerceptionSnapshot.
    This is intentionally simple (late fusion) -- each modality already
    did its own interpretation; we're just merging the results.
    """
    return PerceptionSnapshot(
        person_name=person_name,
        emotion=emotion,
        heard_text=heard_text
    )