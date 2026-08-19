import json
import os
from datetime import datetime

MEMORY_PATH = os.path.join("data", "memory", "people_memory.json")

def _load_memory():
    if not os.path.exists(MEMORY_PATH):
        return {}
    with open(MEMORY_PATH, "r") as f:
        return json.load(f)

def _save_memory(memory):
    os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
    with open(MEMORY_PATH, "w") as f:
        json.dump(memory, f, indent=2)

def remember_encounter(person_name):
    """
    Call this whenever Eli recognizes a known person.
    Updates their visit count and last-seen timestamp.
    """
    if person_name is None:
        return  # nothing to remember for unknown faces

    memory = _load_memory()

    if person_name not in memory:
        memory[person_name] = {
            "visit_count": 0,
            "first_seen": datetime.now().isoformat(),
            "last_seen": None
        }

    memory[person_name]["visit_count"] += 1
    memory[person_name]["last_seen"] = datetime.now().isoformat()

    _save_memory(memory)

def get_person_info(person_name):
    """Returns stored info about a person, or None if never met."""
    memory = _load_memory()
    return memory.get(person_name)